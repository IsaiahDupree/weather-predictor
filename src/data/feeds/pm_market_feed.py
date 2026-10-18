"""
Polymarket daily-temperature MarketFeed — read-only discovery + order books.

Source : Polymarket Gamma Markets API (events) + CLOB API (order books)
Docs   : https://docs.polymarket.com
         - Gamma events : GET https://gamma-api.polymarket.com/events
         - CLOB book    : GET https://clob.polymarket.com/book?token_id=...
Auth   : NONE. Both the Gamma events endpoint and the CLOB ``/book`` endpoint are
         public reads — no API key, no L2 signing. (Signing is only needed to
         place/cancel orders, which this feed never does.) ``is_available()`` is
         therefore always True.
Cadence: Daily-temperature events are created one-per-city-per-day and resolve at
         the city's noon-UTC-ish ``endDate``. Order books update continuously;
         polling ``quotes()`` every few seconds is fine for nowcasting.

Discovery (verified live 2026-05-30):
    Daily highest-temperature events are tagged ``highest-temperature`` (and also
    ``daily-temperature``). They are *not* surfaced by the default events listing
    (which is volume-ranked), so we query::

        GET /events?closed=false&tag_slug=highest-temperature&limit=...

    and keep the ones whose slug names the requested city. Slugs look like
    ``highest-temperature-in-nyc-on-may-30`` /
    ``highest-temperature-in-hong-kong-on-may-29-2026``.

Event / market shape (verified live against event id 25343 + Hong Kong/Jinan):
    event.slug, event.id, event.endDate (ISO-8601 Z), event.markets[] where each
    market has:
        groupItemTitle : the bucket label, e.g. "67°F or below", "68-69°F",
                         "78°F or higher", or single-degree "25°C" for °C cities.
        question       : full text, e.g. "Will the highest temperature in New
                         York City be between 68-69°F on May 30?"
        clobTokenIds   : a JSON-*string* "[\"<yesId>\", \"<noId>\"]".
        outcomes       : JSON-string '["Yes", "No"]'.
        endDate        : per-market resolution time.

Order book shape (verified live against clob.polymarket.com/book):
    {"asks":[{"price":"0.999","size":"102.3"}, ...],
     "bids":[{"price":"0.40","size":"50"}, ...], ...}
    NB: ``price`` / ``size`` arrive as *strings*. Empirically the API returns
    ``asks`` in *descending* price order (best/lowest ask last) and ``bids`` in
    descending order (best/highest bid first) — so we re-sort defensively rather
    than trusting the documented ordering. Prices are already probabilities in
    [0,1]. A resolved/unknown token yields {"error": ...}; treated as empty book.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx

from src.market.snapshot import BucketQuote
from src.market.venue_dto import MarketFeed, RawBucket, RawMarket

try:  # config is optional at import time
    from src.config import get_config
except Exception:  # pragma: no cover - defensive
    get_config = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"
_HTTP_TIMEOUT = 10.0

# Tags Polymarket attaches to the daily highest-temperature events.
_TEMP_TAG_SLUGS = ("highest-temperature", "daily-temperature")

# Polymarket runs its Asian daily-temperature markets in Celsius; US/EU in °F.
# Match against the city slug fragment we send to discover().
_CELSIUS_CITY_HINTS = (
    "jinan", "zhengzhou", "hong-kong", "hongkong", "beijing", "shanghai",
    "guangzhou", "shenzhen", "tokyo", "osaka", "seoul", "taipei", "singapore",
    "bangkok", "mumbai", "delhi", "wuhan", "chengdu", "chongqing", "tianjin",
)

# Bucket-label parsers. Labels come from ``groupItemTitle``; the question text is
# the fallback. Examples handled:
#   "67°F or below"     -> (None, 68)        [inclusive 67 -> hi exclusive 68]
#   "68-69°F"           -> (68, 70)
#   "78°F or higher"    -> (78, None)
#   "25°C"              -> (25, 26)          [single-degree °C bucket]
_DEG = r"[°º]?\s*[FCfc]?"  # optional degree sign + optional F/C unit
_RANGE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*" + _DEG + r"?\s*[-–to]+\s*(-?\d+(?:\.\d+)?)\s*" + _DEG
)
_OR_BELOW_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*" + _DEG + r"?\s*or\s*below", re.I)
_OR_HIGHER_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*" + _DEG + r"?\s*or\s*(?:higher|above|more)", re.I
)
_SINGLE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*" + _DEG + r"\s*$")


def _parse_bounds(label: str, question: str = "") -> Tuple[Optional[float], Optional[float]]:
    """Parse a bucket label into half-open [lo, hi) bounds (market temp units).

    Polymarket buckets are integer-degree-aligned: "68-69" means the high was
    68 or 69, i.e. [68, 70). "67 or below" is everything < 68 -> (None, 68).
    "78 or higher" is >= 78 -> (78, None). A bare "25" (°C cities) is the single
    degree [25, 26). Returns (None, None) if nothing parses.
    """
    text = (label or "").strip()
    for src in (text, question or ""):
        if not src:
            continue
        m = _RANGE_RE.search(src)
        if m:
            lo = float(m.group(1))
            hi = float(m.group(2))
            return (lo, hi + 1.0)  # hi is inclusive in the label -> exclusive edge
        m = _OR_BELOW_RE.search(src)
        if m:
            return (None, float(m.group(1)) + 1.0)
        m = _OR_HIGHER_RE.search(src)
        if m:
            return (float(m.group(1)), None)
        m = _SINGLE_RE.match(src)
        if m:
            v = float(m.group(1))
            return (v, v + 1.0)
    return (None, None)


def _parse_iso_epoch(value: Optional[str]) -> float:
    """ISO-8601 (e.g. '2026-05-29T12:00:00Z') -> epoch seconds UTC. 0.0 on fail."""
    if not value:
        return 0.0
    txt = str(value).strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _json_list(value) -> list:
    """Decode a Gamma JSON-string field (clobTokenIds/outcomes) to a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _to_float(value) -> Optional[float]:
    """Coerce a book price/size (string or number) to float; None if invalid."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or abs(f) == float("inf"):  # NaN / inf guard
        return None
    return f


class PolymarketMarketFeed(MarketFeed):
    """Read-only Polymarket feed for daily highest-temperature events.

    ``discover(city)`` finds the open temperature events for a city via the Gamma
    events API; ``quotes(market)`` reads each bucket's YES and NO order books from
    the public CLOB ``/book`` endpoint. No credentials are required.
    """

    name = "polymarket_market"
    venue = "polymarket"

    def __init__(self, config=None) -> None:
        if config is not None:
            self._config = config
        elif get_config is not None:
            try:
                self._config = get_config()
            except Exception:  # pragma: no cover - config optional for reads
                self._config = None
        else:
            self._config = None

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        # Public reads only — always available.
        return True

    def _units_for_city(self, city: str) -> str:
        slug_frag = self._city_slug(city)
        return "C" if any(h in slug_frag for h in _CELSIUS_CITY_HINTS) else "F"

    @staticmethod
    def _city_slug(city: str) -> str:
        """Normalize a city name into the slug fragment Polymarket uses.

        e.g. "New York City" -> "new-york-city", "NYC" -> "nyc",
        "Hong Kong" -> "hong-kong".
        """
        s = (city or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        return s.strip("-")

    # ------------------------------------------------------------------

    async def discover(self, city: str) -> List[RawMarket]:
        """Return the open daily highest-temperature events for ``city``.

        Queries the Gamma events API by temperature tag and keeps the events
        whose slug names the requested city. Each event becomes one RawMarket
        whose buckets are its constituent binary YES/NO markets.
        """
        city_frag = self._city_slug(city)
        if not city_frag:
            return []

        events: list = []
        seen_ids: set = set()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            for tag_slug in _TEMP_TAG_SLUGS:
                params = {
                    "closed": "false",
                    "active": "true",
                    "tag_slug": tag_slug,
                    "limit": "200",
                }
                try:
                    resp = await client.get(f"{_GAMMA_BASE}/events", params=params)
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("[%s] discover tag=%s failed: %s", self.name, tag_slug, exc)
                    continue
                batch = payload if isinstance(payload, list) else payload.get("data", [])
                for ev in batch or []:
                    if not isinstance(ev, dict):
                        continue
                    ev_id = ev.get("id")
                    if ev_id in seen_ids:
                        continue
                    seen_ids.add(ev_id)
                    events.append(ev)

        markets: List[RawMarket] = []
        for ev in events:
            slug = (ev.get("slug") or "").lower()
            # Match the city as a whole slug segment, not a substring, to avoid
            # e.g. "austin" matching inside another token.
            if city_frag not in slug.split("-on-")[0].split("-"):
                # also accept multi-word city fragments ("hong-kong") appearing intact
                if city_frag not in slug:
                    continue
            if "temperature" not in slug:
                continue
            if ev.get("closed") is True:
                continue

            raw_market = self._build_market(ev, city)
            if raw_market is not None and raw_market.buckets:
                markets.append(raw_market)

        return markets

    def _build_market(self, ev: dict, city: str) -> Optional[RawMarket]:
        event_ticker = ev.get("slug") or str(ev.get("id") or "")
        if not event_ticker:
            return None
        close_time = _parse_iso_epoch(ev.get("endDate"))

        buckets: List[RawBucket] = []
        unit_votes = {"F": 0, "C": 0}
        for m in ev.get("markets", []) or []:
            if not isinstance(m, dict):
                continue
            if m.get("closed") is True:
                continue
            token_ids = _json_list(m.get("clobTokenIds"))
            if len(token_ids) < 1:
                continue
            yes_token = str(token_ids[0]) if token_ids[0] is not None else ""
            if not yes_token:
                continue
            no_token = str(token_ids[1]) if len(token_ids) > 1 and token_ids[1] is not None else None

            label = (m.get("groupItemTitle") or "").strip()
            question = m.get("question") or ""
            if not label:
                label = question.strip() or yes_token[:8]
            lo, hi = _parse_bounds(label, question)

            # Vote on units from the actual market text (°C/°F glyphs) — more
            # reliable than guessing from the city name (e.g. London is °C).
            blob = f"{label} {question}"
            if "°C" in blob or "ºC" in blob:
                unit_votes["C"] += 1
            elif "°F" in blob or "ºF" in blob:
                unit_votes["F"] += 1

            buckets.append(
                RawBucket(
                    label=label,
                    lo=lo,
                    hi=hi,
                    yes_symbol=yes_token,
                    no_token_id=no_token,
                )
            )

        if not buckets:
            return None

        # Units: trust the buckets' own °C/°F glyphs; fall back to a city hint.
        if unit_votes["C"] > unit_votes["F"]:
            units = "C"
        elif unit_votes["F"] > unit_votes["C"]:
            units = "F"
        else:
            units = self._units_for_city(city)

        if not close_time:
            # Fall back to the first market's endDate if the event lacked one.
            for m in ev.get("markets", []) or []:
                close_time = _parse_iso_epoch(m.get("endDate"))
                if close_time:
                    break

        return RawMarket(
            venue=self.venue,
            city=city,
            event_ticker=event_ticker,
            close_time=close_time,
            buckets=buckets,
            units=units,
            raw=ev,
        )

    # ------------------------------------------------------------------

    async def quotes(self, market: RawMarket) -> List[BucketQuote]:
        """Read live YES + NO order books for every bucket of ``market``.

        For each bucket we fetch the CLOB ``/book`` for the YES token and the NO
        token concurrently, deriving bid/ask + ascending ask-side depth. Buckets
        whose books error out (e.g. resolved/expired tokens) yield an all-zero
        quote rather than failing the whole event.
        """
        if not market.buckets:
            return []

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            tasks = [self._quote_for_bucket(client, b) for b in market.buckets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        quotes: List[BucketQuote] = []
        for bucket, res in zip(market.buckets, results):
            if isinstance(res, Exception):
                log.warning("[%s] book fetch failed for %s: %s", self.name, bucket.label, res)
                quotes.append(
                    BucketQuote(
                        bucket=bucket.to_bucket(),
                        symbol=bucket.yes_symbol,
                        token_id_no=bucket.no_token_id,
                    )
                )
            else:
                quotes.append(res)
        return quotes

    async def _quote_for_bucket(
        self, client: httpx.AsyncClient, bucket: RawBucket
    ) -> BucketQuote:
        yes_book = await self._fetch_book(client, bucket.yes_symbol)
        no_book = (
            await self._fetch_book(client, bucket.no_token_id)
            if bucket.no_token_id
            else {"bids": [], "asks": []}
        )

        yes_levels = self._ask_levels(yes_book.get("asks"))
        yes_bids = self._bid_levels(yes_book.get("bids"))
        no_levels = self._ask_levels(no_book.get("asks"))
        no_bids = self._bid_levels(no_book.get("bids"))

        yes_ask = yes_levels[0][0] if yes_levels else 0.0
        yes_ask_size = yes_levels[0][1] if yes_levels else 0.0
        yes_bid = yes_bids[0][0] if yes_bids else 0.0
        yes_bid_size = yes_bids[0][1] if yes_bids else 0.0

        no_ask = no_levels[0][0] if no_levels else 0.0
        no_ask_size = no_levels[0][1] if no_levels else 0.0
        no_bid = no_bids[0][0] if no_bids else 0.0
        no_bid_size = no_bids[0][1] if no_bids else 0.0

        return BucketQuote(
            bucket=bucket.to_bucket(),
            symbol=bucket.yes_symbol,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_ask_levels=yes_levels,
            no_ask_levels=no_levels,
            yes_bid_size=yes_bid_size,
            yes_ask_size=yes_ask_size,
            no_bid_size=no_bid_size,
            no_ask_size=no_ask_size,
            token_id_no=bucket.no_token_id,
        )

    async def _fetch_book(self, client: httpx.AsyncClient, token_id: Optional[str]) -> dict:
        """GET /book for one token. Returns {} (empty book) on error/resolved."""
        if not token_id:
            return {"bids": [], "asks": []}
        try:
            resp = await client.get(f"{_CLOB_BASE}/book", params={"token_id": token_id})
            resp.raise_for_status()
            book = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("[%s] /book error for %s: %s", self.name, token_id, exc)
            return {"bids": [], "asks": []}
        if not isinstance(book, dict) or book.get("error"):
            return {"bids": [], "asks": []}
        return book

    @staticmethod
    def _ask_levels(levels) -> List[Tuple[float, float]]:
        """Normalize a book asks array into ascending (price, size) tuples.

        The CLOB API returns asks in descending price order with string fields;
        ``yes_ask_levels`` must be ascending (best ask first), so we sort.
        """
        out: List[Tuple[float, float]] = []
        for lvl in levels or []:
            if not isinstance(lvl, dict):
                continue
            price = _to_float(lvl.get("price"))
            size = _to_float(lvl.get("size"))
            if price is None or size is None or size <= 0.0:
                continue
            if not (0.0 <= price <= 1.0):
                continue
            out.append((price, size))
        out.sort(key=lambda x: x[0])  # ascending: best (lowest) ask first
        return out

    @staticmethod
    def _bid_levels(levels) -> List[Tuple[float, float]]:
        """Normalize a book bids array into descending (price, size) tuples."""
        out: List[Tuple[float, float]] = []
        for lvl in levels or []:
            if not isinstance(lvl, dict):
                continue
            price = _to_float(lvl.get("price"))
            size = _to_float(lvl.get("size"))
            if price is None or size is None or size <= 0.0:
                continue
            if not (0.0 <= price <= 1.0):
                continue
            out.append((price, size))
        out.sort(key=lambda x: x[0], reverse=True)  # descending: best (highest) bid first
        return out
