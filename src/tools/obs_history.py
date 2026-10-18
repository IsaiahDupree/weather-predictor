"""
obs_history — batched historical daily-high fetch from IEM ASOS (free, no key).

One HTTP request returns a whole date range, so we fetch a station's entire
window in a single call (rate-limit friendly; the per-day loop trips IEM's 429)
and reduce to the max air temp per *local* climate day. Results are cached on
disk so repeated basis fits don't re-hit the network.

Used by the cross-venue basis model (needs many paired station-days) and any
backtest that wants resolved daily highs in bulk.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Dict

import httpx

from src.config import get_config

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_MISSING = {"", "null", "none", "m", "t", "-99", "-99.0", "-99.00", "nan"}


def _cache_path(station: str, start: _dt.date, end: _dt.date) -> Path:
    cfg = get_config()
    d = Path(cfg.log_dir) / "obs_history"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{station.upper()}_{start.isoformat()}_{end.isoformat()}.json"


async def daily_highs_range(
    station: str, tz_name: str, start: _dt.date, end: _dt.date, *, use_cache: bool = True
) -> Dict[str, float]:
    """{local_date_iso: max_tmpf} for [start, end] inclusive, one IEM request.

    Each observation's UTC timestamp is bucketed by its *local* (tz_name) date,
    so the daily high respects the local climate day rather than the UTC day.
    """
    station = station.strip().upper()
    cache = _cache_path(station, start, end)
    if use_cache and cache.exists():
        return json.loads(cache.read_text())

    cfg = get_config()
    # Pad one day each side (UTC vs local offset) so edge days are complete.
    q_start = start - _dt.timedelta(days=1)
    q_end = end + _dt.timedelta(days=2)
    params = [
        ("station", station), ("data", "tmpf"), ("tz", "UTC"),
        ("format", "onlycomma"), ("latlon", "no"), ("missing", "null"), ("trace", "null"),
        ("year1", str(q_start.year)), ("month1", str(q_start.month)), ("day1", str(q_start.day)),
        ("hour1", "0"), ("minute1", "0"),
        ("year2", str(q_end.year)), ("month2", str(q_end.month)), ("day2", str(q_end.day)),
        ("hour2", "0"), ("minute2", "0"),
    ]
    headers = {"User-Agent": cfg.iem_user_agent, "Accept": "text/csv, text/plain"}
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        resp = await client.get(_ASOS_URL, params=params)
        resp.raise_for_status()
        body = resp.text

    tz = ZoneInfo(tz_name) if ZoneInfo else _dt.timezone.utc
    highs: Dict[str, float] = {}
    for line in body.splitlines()[1:]:
        cols = line.split(",")
        if len(cols) < 3:
            continue
        valid, tval = cols[1].strip(), cols[2].strip()
        if tval.lower() in _MISSING:
            continue
        try:
            t = float(tval)
        except ValueError:
            continue
        if t <= -98.0:
            continue
        dt_utc = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt_utc = _dt.datetime.strptime(valid, fmt).replace(tzinfo=_dt.timezone.utc)
                break
            except ValueError:
                continue
        if dt_utc is None:
            continue
        local_day = dt_utc.astimezone(tz).date()
        if local_day < start or local_day > end:
            continue
        key = local_day.isoformat()
        if key not in highs or t > highs[key]:
            highs[key] = round(t, 1)

    if use_cache:
        cache.write_text(json.dumps(highs, sort_keys=True))
    return highs
