"""
ObsTracker — keeps a short rolling history of station observations so the
catalog can compute the two things a single latest-value can't:

  * ``daily_high`` — the maximum temperature observed within the climate-day
    window (the value a daily-high market resolves toward, "so far"), and
  * ``trend_per_hr`` — the recent warming/cooling rate, which drives the
    nowcast projection of the final high.

Fed from the obs feeds (directly or via the bus). All temperatures are returned
in the units the caller requests (the market's resolution units), using the
ObsTick unit-conversion properties.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

from src.data.market_data_bus import ObsTick


def _to_units(tick: ObsTick, units: str) -> float:
    return tick.temp_f if units.upper() == "F" else tick.temp_c


class ObsTracker:
    def __init__(self, max_history: int = 1000) -> None:
        self._hist: Dict[str, Deque[ObsTick]] = defaultdict(lambda: deque(maxlen=max_history))

    def ingest(self, tick: ObsTick) -> None:
        if tick is None:
            return
        self._hist[tick.station].append(tick)

    def latest(self, station: str) -> Optional[ObsTick]:
        h = self._hist.get(station)
        return h[-1] if h else None

    def _obs_time(self, tick: ObsTick) -> float:
        return tick.obs_time if tick.obs_time is not None else tick.timestamp

    def daily_high(self, station: str, window_start: float, units: str = "F",
                   window_end: Optional[float] = None) -> Optional[float]:
        """Max observed temperature (in ``units``) within [window_start, window_end)."""
        h = self._hist.get(station)
        if not h:
            return None
        end = window_end if window_end is not None else time.time() + 1.0
        vals = [_to_units(t, units) for t in h if window_start <= self._obs_time(t) < end]
        return max(vals) if vals else None

    def trend_per_hr(self, station: str, units: str = "F", lookback_s: float = 3600.0) -> Optional[float]:
        """Least-squares slope (°/hour, in ``units``) over the last ``lookback_s``."""
        h = self._hist.get(station)
        if not h or len(h) < 2:
            return None
        now = time.time()
        pts: List[Tuple[float, float]] = [
            (self._obs_time(t), _to_units(t, units))
            for t in h if now - self._obs_time(t) <= lookback_s
        ]
        if len(pts) < 2:
            return None
        t0 = pts[0][0]
        xs = [(t - t0) / 3600.0 for t, _ in pts]   # hours
        ys = [v for _, v in pts]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 1e-9:
            return 0.0
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        return num / denom

    def history(self, station: str) -> List[ObsTick]:
        return list(self._hist.get(station, []))
