"""
End-to-end loop in SIM mode with in-memory venue + forecast (no network).

Exercises the full pipeline: ObsTracker → EventCatalog (FakeMarketFeed +
FakeForecast) → strategies → sizing → risk gate → router → SimAdapter, then
settlement. No mocks of our own code; the fakes implement the real MarketFeed /
ForecastClient contracts.
"""

import time
from datetime import date
from pathlib import Path

from src.config import Config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult
from src.data.market_data_bus import ObsTick
from src.data.obs_tracker import ObsTracker
from src.execution.adapters.sim import SimAdapter
from src.execution.router import ExecutionRouter
from src.market.bucket import BucketGrid
from src.market.event_catalog import EventCatalog
from src.market.snapshot import BucketQuote
from src.market.venue_dto import MarketFeed, RawBucket, RawMarket
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits
from src.risk.risk_engine import RiskEngine
from src.runtime.loop import WeatherBotLoop
from src.strategies.intra_market_arb import IntraMarketArb
from src.strategies.tail_bucket_sniper import TailBucketSniper

_GRID = BucketGrid.kalshi_style(69, 81, 2)


class FakeKalshiFeed(MarketFeed):
    name = "fake_kalshi"
    venue = "kalshi"

    def __init__(self, yes_asks):
        self._yes_asks = yes_asks   # {label: ask_price}

    async def discover(self, city):
        buckets = [RawBucket(label=b.label, lo=b.lo, hi=b.hi, yes_symbol=f"KXHIGHNY-{b.label}")
                   for b in _GRID]
        return [RawMarket(venue="kalshi", city=city, event_ticker="KXHIGHNY-26X",
                          close_time=time.time() + 3600, buckets=buckets, units="F")]

    async def quotes(self, market):
        out = []
        for rb in market.buckets:
            ya = self._yes_asks.get(rb.label, 0.5)
            out.append(BucketQuote(bucket=rb.to_bucket(), symbol=rb.yes_symbol,
                                   yes_ask=ya, yes_bid=max(0.0, ya - 0.02),
                                   no_ask=1 - ya + 0.02, no_bid=1 - ya,
                                   yes_ask_levels=[(ya, 500.0)], yes_ask_size=500.0))
        return out


class FakeForecast(ForecastClient):
    name = "fake_forecast"

    def __init__(self, members):
        self._members = members

    async def daily_tmax(self, lat, lon, target_date, units="F"):
        return ForecastResult(source=self.name, target_date=str(target_date),
                              units=units, members=list(self._members))


def _build_loop(tmp_path, yes_asks, members, strategies):
    config = Config(cities=["nyc"], mode="sim", default_venue="kalshi",
                    bankroll_usd=1000.0, min_ev_threshold=0.02, log_dir=tmp_path,
                    kill_switch_file=tmp_path / "KILL")
    sim = SimAdapter(starting_cash=1000.0)
    ks = KillSwitch(tmp_path / "KILL"); ks.reset()
    risk = RiskEngine(limits=RiskLimits.from_config(config), kill_switch=ks)
    router = ExecutionRouter(risk, event_log_path=tmp_path / "ev.jsonl")
    router.register_adapter(sim, venue="sim")

    tracker = ObsTracker()
    # Seed a rising temperature history for Central Park (KNYC).
    now = time.time()
    for i, t in enumerate([70, 72, 74, 75]):
        tracker.ingest(ObsTick(station="KNYC", temp=float(t), units="F",
                               obs_time=now - (3 - i) * 600, source="test"))

    catalog = EventCatalog(FakeKalshiFeed(yes_asks), tracker, [FakeForecast(members)])
    loop = WeatherBotLoop(config, router, tracker, {"kalshi": catalog},
                          strategies, obs_feeds=[])
    return loop, sim


async def test_tail_sniper_routes_and_fills_in_sim(tmp_path):
    # Forecast members cluster high (78-80) but those buckets are priced cheap.
    members = [78, 79, 79, 80, 80, 81, 79, 78, 80, 79]
    yes_asks = {"79-80": 0.05, "77-78": 0.06, ">=81": 0.04}
    loop, sim = _build_loop(tmp_path, yes_asks, members, [TailBucketSniper()])
    stats = await loop.run_once()
    assert stats["signals_seen"] > 0
    assert stats["orders_submitted"] > 0
    assert stats["orders_filled"] > 0
    # cash decreased by what we deployed; positions opened
    assert sim.get_cash() < 1000.0
    positions = await sim.get_positions()
    assert positions


async def test_arb_routes_all_legs_and_settles(tmp_path):
    # Every bucket YES ask = 0.10 -> 8 * 0.10 = 0.80 < 1 -> Dutch book.
    yes_asks = {b.label: 0.10 for b in _GRID}
    loop, sim = _build_loop(tmp_path, yes_asks, [75, 75, 76], [IntraMarketArb()])
    stats = await loop.run_once()
    assert stats["orders_filled"] == len(_GRID)     # one filled leg per bucket
    # Exactly one bucket wins at resolution; the set paid 0.80 and collects 1.00.
    spent = 1000.0 - sim.get_cash()
    win_label = _GRID.which(79.4).label
    rec = await sim.resolve(f"KXHIGHNY-{win_label}", won=True)
    # flatten the rest as losers
    for b in _GRID:
        if b.label != win_label:
            await sim.resolve(f"KXHIGHNY-{b.label}", won=False)
    # net: paid ~0.80*qty, collected 1.00*qty on the winning leg
    assert rec.pnl > 0
    assert sim.realized_pnl() > 0      # risk-free Dutch book books a profit


async def test_selfcheck_smoke(tmp_path):
    # The loop's run_once must be safe to call twice and keep counters monotonic.
    yes_asks = {"79-80": 0.05}
    loop, _ = _build_loop(tmp_path, yes_asks, [79, 80, 79, 80], [TailBucketSniper()])
    s1 = await loop.run_once()
    s2 = await loop.run_once()
    assert s2["cycles"] == 2 and s2["signals_seen"] >= s1["signals_seen"]
