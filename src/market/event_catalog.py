"""
EventCatalog — assembles MarketSnapshots from the live data sources.

It is the join point: a venue MarketFeed supplies the open events + quotes, the
ObsTracker supplies the daily-high-so-far + trend from raw station obs, and the
forecast clients supply the per-bucket probability distribution. Everything is
dependency-injected against the abstract contracts (MarketFeed, ForecastClient,
ObsTracker), so the catalog never imports a concrete venue/feed module — the
runtime factory wires the concrete classes in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from src.data.feeds.forecast_base import ForecastClient
from src.data.obs_tracker import ObsTracker
from src.edge.distribution import ensemble_to_bucket_probs, normal_to_bucket_probs
from src.market.bucket import BucketGrid
from src.market.fees import worst_case_fee_per_contract
from src.market.resolution import get_resolution
from src.market.snapshot import MarketSnapshot
from src.market.venue_dto import MarketFeed, RawMarket

log = logging.getLogger(__name__)

_DEFAULT_SIGMA = {"F": 2.5, "C": 1.4}   # fallback forecast/nowcast spread


class EventCatalog:
    def __init__(
        self,
        market_feed: MarketFeed,
        obs_tracker: ObsTracker,
        forecast_clients: Sequence[ForecastClient],
        *,
        prob_floor: float = 0.002,
    ) -> None:
        self.market_feed = market_feed
        self.obs_tracker = obs_tracker
        self.forecast_clients = [c for c in forecast_clients if c and c.is_available()]
        self.prob_floor = prob_floor

    # ------------------------------------------------------------------

    async def snapshots_for_city(self, city: str) -> List[MarketSnapshot]:
        try:
            markets = await self.market_feed.discover(city)
        except Exception as exc:
            log.warning("[catalog] discover %s on %s failed: %s", city, self.market_feed.venue, exc)
            return []
        snaps: List[MarketSnapshot] = []
        for m in markets:
            try:
                snap = await self.build_snapshot(m)
            except Exception:
                log.exception("[catalog] build_snapshot failed for %s", m.event_ticker)
                snap = None
            if snap is not None:
                snaps.append(snap)
        return snaps

    async def build_snapshot(self, market: RawMarket) -> Optional[MarketSnapshot]:
        res = get_resolution(market.venue, market.city)
        if res is None:
            # No known resolution station — the risk engine would reject anyway.
            log.info("[catalog] no resolution spec for (%s,%s); skipping", market.venue, market.city)
            return None
        if not market.buckets:
            return None

        grid = BucketGrid([b.to_bucket() for b in market.buckets])
        quotes = await self.market_feed.quotes(market)
        if not quotes:
            return None

        now = time.time()
        station = res.station
        units = res.units

        # --- raw obs context ---
        latest = self.obs_tracker.latest(station)
        obs_temp = None
        obs_age = None
        if latest is not None:
            obs_temp = latest.temp_f if units == "F" else latest.temp_c
            obs_age = now - (latest.obs_time if latest.obs_time is not None else latest.timestamp)

        win_start, win_end = res.day_window()
        win_start_e, win_end_e = win_start.timestamp(), win_end.timestamp()
        daily_high = self.obs_tracker.daily_high(station, win_start_e, units, win_end_e)
        trend = self.obs_tracker.trend_per_hr(station, units)

        # --- forecast distribution over buckets ---
        target_date = self._climate_date(market.close_time, res.tz)
        members, points = await self._gather_forecast(res.latlon, target_date, units)
        resid_sigma = _DEFAULT_SIGMA.get(units, 2.5)
        if len(members) >= 2:
            forecast_q = ensemble_to_bucket_probs(members, grid, floor=self.prob_floor)
            resid_sigma = max(0.5, _stddev(members))
        elif points:
            mu = sum(points) / len(points)
            sigma = max(_DEFAULT_SIGMA.get(units, 2.5), _stddev(points) if len(points) > 1 else 0.0)
            forecast_q = normal_to_bucket_probs(mu, sigma, grid, floor=self.prob_floor)
            resid_sigma = sigma
        else:
            forecast_q = {}

        seconds_to_res = max(0.0, market.close_time - now)
        span = max(1.0, win_end_e - win_start_e)
        frac_elapsed = min(1.0, max(0.0, (now - win_start_e) / span))

        return MarketSnapshot(
            venue=market.venue,
            city=market.city,
            event_ticker=market.event_ticker,
            resolution=res,
            grid=grid,
            quotes=quotes,
            forecast_q=forecast_q,
            obs_temp=obs_temp,
            obs_age_s=obs_age,
            daily_high_so_far=daily_high,
            seconds_to_resolution=seconds_to_res,
            fraction_of_day_elapsed=frac_elapsed,
            fee_per_contract=worst_case_fee_per_contract(market.venue),
            trend_per_hr=trend,
            resid_sigma=resid_sigma,
            ts=now,
        )

    # ------------------------------------------------------------------

    async def _gather_forecast(self, latlon, target_date, units):
        lat, lon = latlon
        members: List[float] = []
        points: List[float] = []
        if not self.forecast_clients or (lat == 0.0 and lon == 0.0):
            return members, points
        results = await asyncio.gather(
            *[c.daily_tmax(lat, lon, target_date, units) for c in self.forecast_clients],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            if getattr(r, "members", None):
                members.extend(r.members)
            elif getattr(r, "point", None) is not None:
                points.append(r.point)
        return members, points

    def _climate_date(self, close_time: float, tz: str):
        """The climate-day date a market resolves on, derived from its close time.

        Localize the midpoint of the climate day (close - 12h) to the station's
        timezone so we land on the correct date regardless of the UTC boundary.
        """
        ref = close_time - 12 * 3600.0
        if ZoneInfo is not None:
            dt = datetime.fromtimestamp(ref, tz=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
        else:  # pragma: no cover
            dt = datetime.utcfromtimestamp(ref)
        return dt.date()


def _stddev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
