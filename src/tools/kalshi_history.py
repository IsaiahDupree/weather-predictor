"""
kalshi_history — public, read-only fetch of SETTLED Kalshi weather markets and
their historical candlestick prices.

Both endpoints are public (no auth):
  GET /events?series_ticker=S&status=settled&with_nested_markets=true  -> results
  GET /series/{S}/markets/{ticker}/candlesticks?period_interval=60&...  -> OHLC

Candlestick numeric fields arrive as ``*_dollars`` strings (probabilities in
[0,1], e.g. "0.0200"). We return the YES mid (bid/ask close) at a chosen lead
time before each market's close, paired with the realized result — the raw
material for a market-calibration backtest.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Dict, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

_PROD = "https://api.elections.kalshi.com/trade-api/v2"
_TIMEOUT = 20.0


async def get_json(client: httpx.AsyncClient, url: str, params: dict,
                   *, retries: int = 5) -> dict:
    """GET with exponential backoff on 429/5xx (Kalshi public API rate-limits)."""
    delay = 1.0
    for attempt in range(retries):
        r = await client.get(url, params=params, headers={"Accept": "application/json"})
        if r.status_code in (429, 500, 502, 503):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 16.0)
            continue
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, dict) else {}
    r.raise_for_status()
    return {}


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def settled_events(client: httpx.AsyncClient, series: str, limit: int = 200) -> List[dict]:
    """Settled events (with nested markets carrying ``result`` + close_time)."""
    d = await get_json(client, f"{_PROD}/events", {
        "series_ticker": series, "status": "settled",
        "with_nested_markets": "true", "limit": limit,
    })
    return d.get("events", []) if isinstance(d, dict) else []


def _parse_iso(value) -> Optional[float]:
    if not value:
        return None
    t = str(value).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


async def candlestick_at(
    client: httpx.AsyncClient, series: str, ticker: str,
    close_ts: float, lead_hours: float, max_drift_hours: float = 4.0,
) -> Optional[Dict[str, float]]:
    """{mid, bid, ask, volume} from the hourly candle nearest (close - lead_hours).

    bid/ask are the candle's YES bid/ask close (for honest fill prices); mid
    falls back across price/mean. Returns None if no usable candle lies within
    ``max_drift_hours`` of the target (so a missing far-out price is NOT silently
    replaced by a near-close one).
    """
    target = close_ts - lead_hours * 3600.0
    # Window wide enough to span the requested lead even when it's >24h out.
    start = int(close_ts - max(30.0, lead_hours + 6.0) * 3600.0)
    end = int(close_ts + 3600.0)
    try:
        d = await get_json(
            client, f"{_PROD}/series/{series}/markets/{ticker}/candlesticks",
            {"period_interval": 60, "start_ts": start, "end_ts": end})
        candles = d.get("candlesticks", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("candles %s failed: %s", ticker, exc)
        return None

    best = None
    best_dt = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        mid = _candle_mid(c)
        if mid is None:
            continue
        dist = abs(ts - target)
        if best_dt is None or dist < best_dt:
            best_dt = dist
            best = {
                "mid": mid,
                "bid": _f((c.get("yes_bid") or {}).get("close_dollars")),
                "ask": _f((c.get("yes_ask") or {}).get("close_dollars")),
                "volume": _f(c.get("volume_fp")) or 0.0,
            }
    if best_dt is not None and best_dt > max_drift_hours * 3600.0:
        return None
    return best


def _candle_mid(c: dict) -> Optional[float]:
    yb = (c.get("yes_bid") or {}).get("close_dollars")
    ya = (c.get("yes_ask") or {}).get("close_dollars")
    b, a = _f(yb), _f(ya)
    if b is not None and a is not None and a >= b:
        return round((a + b) / 2.0, 4)
    price = (c.get("price") or {})
    for k in ("close_dollars", "mean_dollars", "previous_dollars"):
        v = _f(price.get(k))
        if v is not None:
            return round(v, 4)
    return b if b is not None else a


def market_result(m: dict) -> Optional[int]:
    """1 if the bucket resolved YES, 0 if NO, None if not settled."""
    res = str(m.get("result") or "").strip().lower()
    if res == "yes":
        return 1
    if res == "no":
        return 0
    return None
