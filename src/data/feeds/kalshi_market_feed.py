"""
KalshiMarketFeed — read-only discovery + order-book quotes for Kalshi's daily
high-temperature prediction markets.

Source : Kalshi Trade API v2 (public, *read-only*; the GetEvents / GetMarkets /
         GetMarketOrderbook endpoints used here require no authentication).
Docs   : https://docs.kalshi.com/api-reference/market/get-events
         https://docs.kalshi.com/api-reference/market/get-markets
         https://docs.kalshi.com/api-reference/market/get-market-orderbook
Cadence: order books update continuously; poll no faster than ~1-2 s/market.
         The event/market catalog only needs refreshing a few times an hour.

Hosts (verified May 2026):
    prod : https://api.elections.kalshi.com/trade-api/v2   (config.kalshi_env == "prod")
    demo : https://demo-api.kalshi.co/trade-api/v2          (everything else)

Daily-high series tickers (verified live against GET /series/{ticker}):
    nyc          -> KXHIGHNY    ("Highest temperature in NYC")
    chicago      -> KXHIGHCHI   ("Highest temperature in Chicago")
    los angeles  -> KXHIGHLAX   ("Highest temperature in Los Angeles")
    miami        -> KXHIGHMIA   ("Highest temperature in Miami")
    denver       -> KXHIGHDEN   ("Highest temperature in Denver")
    austin       -> KXHIGHAUS   ("Highest temperature in Austin")
    philadelphia -> KXHIGHPHIL  ("Highest temperature in Philadelphia")
Temperatures are always °F (US NWS Daily Climate Report settlement).

Discovery flow (all GETs, no body):
    GET /events?series_ticker={S}&status=open&with_nested_markets=true&limit=200
        -> {"events": [{event_ticker, series_ticker, markets: [...]}], "cursor": ...}
    Each nested market carries: ticker, strike_type, floor_strike, cap_strike,
    close_time (ISO-8601 UTC, e.g. "2026-06-01T04:59:00Z"), yes_sub_title,
    subtitle, status. (If a server omits nested markets we fall back to
    GET /markets?event_ticker={E}&status=open.)

Strike -> half-open bucket [lo, hi) in °F (Bucket/RawBucket convention; lo
inclusive, hi exclusive, None = open end). Verified semantics:
    strike_type "less"    cap=C,  "C-1° or below"  -> YES iff Tmax <= C-1  -> [None, C)
    strike_type "between" floor=F,cap=C, "F° to C°" -> YES iff F<=Tmax<=C  -> [F, C+1)
    strike_type "greater" floor=F, "F+1° or above"  -> YES iff Tmax >= F+1 -> [F+1, None)

Quotes flow:
    GET /markets/{ticker}/orderbook?depth=0          (0 = all levels)
        -> {"orderbook_fp": {"yes_dollars": [[price, size], ...],
                             "no_dollars":  [[price, size], ...]}}
    Prices in the *_fp form are already dollars i.e. probabilities in [0,1]
    (e.g. "0.0400" == 4¢); older deployments return {"orderbook": {"yes": [[cents,
    size]], "no": [...]}} with integer cents — both are handled.

    Kalshi order books contain *bids only*. yes_dollars are YES bids, no_dollars
    are NO bids, each sorted ascending by price (best/highest bid last). In a
    binary market a YES bid at price p is economically a NO ask at (1-p), so:
        yes_bid = best (highest) YES bid               (last of yes_dollars)
        no_bid  = best (highest) NO bid                (last of no_dollars)
        yes_ask = 1 - (best NO bid)                    (derived from no_dollars)
        no_ask  = 1 - (best YES bid)                   (derived from yes_dollars)
    The YES ask ladder is the NO bid ladder reflected through 1 and re-sorted
    ascending; likewise the NO ask ladder from the YES bids.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.market.snapshot import BucketQuote
from src.market.venue_dto import MarketFeed, RawBucket, RawMarket

try:  # config is a Tier-0 primitive; guard so import never hard-fails
    from src.config import Config, get_config
except Exception:  # pragma: no cover - defensive
    Config = Any  # type: ignore

    def get_config():  # type: ignore
        return None


log = logging.getLogger(__name__)

_PROD_HOST = "https://api.elections.kalshi.com/trade-api/v2"
_DEMO_HOST = "https://demo-api.kalshi.co/trade-api/v2"

# city (normalized) -> Kalshi daily-high series ticker (verified via GET /series)
_CITY_SERIES: Dict[str, str] = {
    "nyc": "KXHIGHNY",
    "new york": "KXHIGHNY",
    "new york city": "KXHIGHNY",
    "ny": "KXHIGHNY",
    "chicago": "KXHIGHCHI",
    "chi": "KXHIGHCHI",
    "la": "KXHIGHLAX",
    "los angeles": "KXHIGHLAX",
    "lax": "KXHIGHLAX",
    "miami": "KXHIGHMIA",
    "mia": "KXHIGHMIA",
    "denver": "KXHIGHDEN",
    "den": "KXHIGHDEN",
    "austin": "KXHIGHAUS",
    "aus": "KXHIGHAUS",
    "philadelphia": "KXHIGHPHIL",
    "philly": "KXHIGHPHIL",
    "phil": "KXHIGHPHIL",
}

_TIMEOUT = 10.0
# Treat these as "tradeable / open" market statuses.
_OPEN_STATUSES = {"active", "open"}


class KalshiMarketFeed(MarketFeed):
    """Public, read-only Kalshi daily-high temperature market feed.

    Parameters
    ----------
    config:
        Optional :class:`~src.config.Config`; defaults to the process singleton.
        Only ``config.kalshi_env`` is consulted (to pick the prod vs demo host);
        the read endpoints used here need no API key.
    """

    name = "kalshi"
    venue = "kalshi"

    def __init__(self, config: Optional["Config"] = None) -> None:
        self._config = config or get_config()
        env = ""
        try:
            env = str(getattr(self._config, "kalshi_env", "") or "").strip().lower()
        except Exception:  # pragma: no cover - defensive
            env = ""
        self._base = _PROD_HOST if env == "prod" else _DEMO_HOST

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Always available: discovery + quotes are public, key-free reads."""
        return True

    @staticmethod
    def series_for(city: str) -> Optional[str]:
        """Map a city name to its Kalshi daily-high series ticker, or None."""
        return _CITY_SERIES.get((city or "").strip().lower())

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self, city: str) -> List[RawMarket]:
        """Return the currently-open daily-high events for ``city``.

        Returns ``[]`` for an unmapped city or when no open events exist.
        Raises on network/HTTP errors so callers can back off.
        """
        series = self.series_for(city)
        if not series:
            log.debug("kalshi: no daily-high series mapped for city %r", city)
            return []

        params = {
            "series_ticker": series,
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            data = await self._get_json(client, "/events", params)
            events = data.get("events") if isinstance(data, dict) else None
            if not isinstance(events, list):
                return []

            results: List[RawMarket] = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                event_ticker = str(ev.get("event_ticker") or "").strip()
                if not event_ticker:
                    continue

                markets = ev.get("markets")
                if not isinstance(markets, list) or not markets:
                    # Fall back to the dedicated markets endpoint.
                    markets = await self._fetch_event_markets(client, event_ticker)

                buckets: List[RawBucket] = []
                close_epoch = 0.0
                for m in markets:
                    if not isinstance(m, dict):
                        continue
                    if not self._is_open_market(m):
                        continue
                    rb = self._market_to_bucket(m)
                    if rb is None:
                        continue
                    buckets.append(rb)
                    ce = self._parse_iso_epoch(m.get("close_time"))
                    if ce is not None and ce > close_epoch:
                        close_epoch = ce

                if not buckets:
                    continue

                results.append(
                    RawMarket(
                        venue=self.venue,
                        city=city,
                        event_ticker=event_ticker,
                        close_time=close_epoch,
                        buckets=buckets,
                        units="F",
                        raw=ev,
                    )
                )
            return results

    async def _fetch_event_markets(
        self, client: httpx.AsyncClient, event_ticker: str
    ) -> List[Dict[str, Any]]:
        params = {"event_ticker": event_ticker, "status": "open", "limit": 200}
        try:
            data = await self._get_json(client, "/markets", params)
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            log.warning("kalshi: GET /markets for %s failed: %s", event_ticker, exc)
            return []
        markets = data.get("markets") if isinstance(data, dict) else None
        return markets if isinstance(markets, list) else []

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    async def quotes(self, market: RawMarket) -> List[BucketQuote]:
        """Read the live order book for each bucket of ``market``.

        One GET /markets/{ticker}/orderbook per bucket. Buckets whose order book
        is empty/unavailable yield a zero-priced quote (so the grid stays
        complete) rather than being dropped.
        """
        out: List[BucketQuote] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for rb in market.buckets:
                ticker = rb.yes_symbol
                if not ticker:
                    continue
                try:
                    data = await self._get_json(
                        client, f"/markets/{ticker}/orderbook", {"depth": 0}
                    )
                except httpx.HTTPError as exc:  # pragma: no cover - network dependent
                    log.warning("kalshi: orderbook %s failed: %s", ticker, exc)
                    out.append(self._empty_quote(rb))
                    continue
                out.append(self._build_quote(rb, data))
        return out

    def _build_quote(self, rb: RawBucket, data: Dict[str, Any]) -> BucketQuote:
        yes_bids, no_bids = self._extract_levels(data)

        # Bids are ascending by price; the best (highest) bid is the last entry.
        yes_bid, yes_bid_size = (yes_bids[-1] if yes_bids else (0.0, 0.0))
        no_bid, no_bid_size = (no_bids[-1] if no_bids else (0.0, 0.0))

        # YES asks are NO bids reflected through 1.0: NO bid (p, s) == YES ask
        # (1 - p, s). Reflecting an ascending NO-bid ladder gives a descending
        # YES-ask ladder, so reverse to keep yes_ask_levels ascending by price.
        yes_ask_levels: List[Tuple[float, float]] = [
            (round(1.0 - p, 6), s) for (p, s) in reversed(no_bids)
        ]
        no_ask_levels: List[Tuple[float, float]] = [
            (round(1.0 - p, 6), s) for (p, s) in reversed(yes_bids)
        ]

        yes_ask, yes_ask_size = (yes_ask_levels[0] if yes_ask_levels else (0.0, 0.0))
        no_ask, no_ask_size = (no_ask_levels[0] if no_ask_levels else (0.0, 0.0))

        return BucketQuote(
            bucket=rb.to_bucket(),
            symbol=rb.yes_symbol,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_ask_levels=yes_ask_levels,
            no_ask_levels=no_ask_levels,
            yes_bid_size=yes_bid_size,
            yes_ask_size=yes_ask_size,
            no_bid_size=no_bid_size,
            no_ask_size=no_ask_size,
            token_id_no=None,
        )

    def _empty_quote(self, rb: RawBucket) -> BucketQuote:
        return BucketQuote(bucket=rb.to_bucket(), symbol=rb.yes_symbol)

    # ------------------------------------------------------------------
    # Order-book parsing
    # ------------------------------------------------------------------

    @classmethod
    def _extract_levels(
        cls, data: Dict[str, Any]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Return (yes_bid_levels, no_bid_levels), ascending, prices in [0,1].

        Handles both the current ``orderbook_fp`` (dollar) form and the legacy
        ``orderbook`` (integer-cents) form. A null/empty book yields empty lists.
        """
        if not isinstance(data, dict):
            return [], []

        ob = data.get("orderbook_fp")
        if isinstance(ob, dict):
            yes = cls._levels(ob.get("yes_dollars"), cents=False)
            no = cls._levels(ob.get("no_dollars"), cents=False)
            return yes, no

        ob = data.get("orderbook")
        if isinstance(ob, dict):
            # Newer schema may nest *_dollars even under "orderbook".
            yes = cls._levels(ob.get("yes_dollars") or ob.get("yes"),
                              cents=("yes_dollars" not in ob))
            no = cls._levels(ob.get("no_dollars") or ob.get("no"),
                             cents=("no_dollars" not in ob))
            return yes, no

        return [], []

    @staticmethod
    def _levels(raw: Any, *, cents: bool) -> List[Tuple[float, float]]:
        """Parse an order-book ladder into ascending (price[0,1], size) tuples."""
        if not isinstance(raw, list):
            return []
        out: List[Tuple[float, float]] = []
        for lvl in raw:
            if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
                continue
            price = _to_float(lvl[0])
            size = _to_float(lvl[1])
            if price is None or size is None:
                continue
            if cents:
                price = price / 100.0
            if size <= 0.0:
                continue
            if not (0.0 <= price <= 1.0):
                continue
            out.append((round(price, 6), size))
        out.sort(key=lambda t: t[0])  # ascending by price (best bid last)
        return out

    # ------------------------------------------------------------------
    # Market -> bucket mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _is_open_market(m: Dict[str, Any]) -> bool:
        status = str(m.get("status") or "").strip().lower()
        # Empty status (some nested payloads omit it) is treated as open.
        return status == "" or status in _OPEN_STATUSES

    @classmethod
    def _market_to_bucket(cls, m: Dict[str, Any]) -> Optional[RawBucket]:
        ticker = str(m.get("ticker") or "").strip()
        if not ticker:
            return None

        floor = _to_float(m.get("floor_strike"))
        cap = _to_float(m.get("cap_strike"))
        strike_type = str(m.get("strike_type") or "").strip().lower()

        lo, hi = cls._half_open_bounds(strike_type, floor, cap)
        if lo is None and hi is None:
            # Couldn't derive any bound -> not a usable threshold bucket.
            return None

        label = (
            str(m.get("yes_sub_title") or m.get("subtitle") or "").strip()
            or cls._fallback_label(lo, hi)
        )
        return RawBucket(label=label, lo=lo, hi=hi, yes_symbol=ticker, no_token_id=None)

    @staticmethod
    def _half_open_bounds(
        strike_type: str, floor: Optional[float], cap: Optional[float]
    ) -> Tuple[Optional[float], Optional[float]]:
        """Map Kalshi strikes (integer °F thresholds) to a half-open [lo, hi).

        Kalshi thresholds are inclusive integers; our Bucket is [lo, hi) with hi
        exclusive, so an inclusive upper edge ``C`` becomes ``C + 1``.
            less     "C-1 or below"  cap=C   -> [None, C)
            between  "F to C"        F,C     -> [F, C + 1)
            greater  "F+1 or above"  floor=F -> [F + 1, None)
        Falls back to using whichever strike(s) are present when strike_type is
        missing/unknown (cap = exclusive upper as-is, floor = inclusive lower+1).
        """
        if strike_type == "less":
            return None, cap
        if strike_type == "greater":
            return (None if floor is None else floor + 1.0), None
        if strike_type == "between":
            hi = None if cap is None else cap + 1.0
            return floor, hi

        # Unknown / absent strike_type: derive from available strikes.
        if floor is not None and cap is not None:
            return floor, cap + 1.0
        if cap is not None:
            return None, cap
        if floor is not None:
            return floor + 1.0, None
        return None, None

    @staticmethod
    def _fallback_label(lo: Optional[float], hi: Optional[float]) -> str:
        if lo is None and hi is not None:
            return f"<={hi - 1:g}"
        if hi is None and lo is not None:
            return f">={lo:g}"
        if lo is not None and hi is not None:
            return f"{lo:g}-{hi - 1:g}"
        return "?"

    # ------------------------------------------------------------------
    # HTTP + parsing helpers
    # ------------------------------------------------------------------

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        url = self._base + path
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_iso_epoch(value: Any) -> Optional[float]:
        """Convert an ISO-8601 timestamp (e.g. '2026-06-01T04:59:00Z') to epoch s."""
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            return dt.timestamp()
        except (OverflowError, OSError, ValueError):
            return None


def _to_float(value: Any) -> Optional[float]:
    """Parse a numeric/string value to float; None for missing/'-99'/null."""
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"null", "none", "-99", "-99.0"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
