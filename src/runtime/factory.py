"""
Factory — wires concrete adapters/feeds/strategies into a ready-to-run loop.

This is the ONLY module that imports the concrete leaf clients (the venue
adapters and feed classes). Every import is lazy + guarded so the bot still
boots and reports a clear status when an optional client/credential is missing
(e.g. the Polymarket SDK isn't installed, or a region's feed isn't configured).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from src.config import Config, get_config
from src.data.obs_tracker import ObsTracker
from src.execution.adapters.base import BaseAdapter
from src.execution.adapters.sim import SimAdapter
from src.execution.router import ExecutionRouter
from src.market.event_catalog import EventCatalog
from src.market.resolution import get_resolution, known_cities
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits
from src.risk.risk_engine import RiskEngine
from src.runtime.loop import WeatherBotLoop
from src.runtime.narrator import Narrator

log = logging.getLogger(__name__)


# ---- strategies ----------------------------------------------------------

def build_strategies(names: Optional[Sequence[str]] = None) -> List:
    from src.strategies.tail_bucket_sniper import TailBucketSniper
    from src.strategies.oracle_lag import OracleLag
    from src.strategies.near_expiry_scalp import NearExpiryScalp
    from src.strategies.intra_market_arb import IntraMarketArb
    from src.strategies.bayesian_intraday import BayesianIntraday

    registry = {
        "tail_bucket_sniper": TailBucketSniper,
        "oracle_lag": OracleLag,
        "near_expiry_scalp": NearExpiryScalp,
        "intra_market_arb": IntraMarketArb,
        "bayesian_intraday": BayesianIntraday,
    }
    chosen = list(names) if names else list(registry)
    return [registry[n]() for n in chosen if n in registry]


# ---- forecast clients ----------------------------------------------------

def build_forecast_clients(config: Config) -> List:
    clients: List = []
    try:
        from src.data.feeds.forecast_openmeteo import OpenMeteoForecast
        clients.append(OpenMeteoForecast(config))
    except Exception as exc:
        log.debug("[factory] OpenMeteoForecast unavailable: %s", exc)
    try:
        from src.data.feeds.forecast_pirate import PirateForecast
        c = PirateForecast(config)
        if c.is_available():
            clients.append(c)
    except Exception as exc:
        log.debug("[factory] PirateForecast unavailable: %s", exc)
    try:
        from src.data.feeds.forecast_vote import VoteForecast
        c = VoteForecast(config)
        if c.is_available():
            clients.append(c)
    except Exception as exc:
        log.debug("[factory] VoteForecast unavailable: %s", exc)
    return [c for c in clients if c is not None and c.is_available()]


# ---- obs feeds -----------------------------------------------------------

_REGION_FEED_MODULE = {
    "tw": ("src.data.feeds.cwa_obs", "CwaObsFeed"),
    "jp": ("src.data.feeds.jma_obs", "JmaObsFeed"),
    "kr": ("src.data.feeds.kma_obs", "KmaObsFeed"),
    "hk": ("src.data.feeds.hko_obs", "HkoObsFeed"),
}


def _make_feed(module: str, cls: str, station: str, config: Config):
    try:
        mod = __import__(module, fromlist=[cls])
        feed = getattr(mod, cls)(station, bus=None, config=config)
        return feed if feed.is_available() else None
    except Exception as exc:
        log.debug("[factory] feed %s.%s unavailable: %s", module, cls, exc)
        return None


def build_obs_feeds(config: Config, venue: str) -> List:
    """One or more obs feeds per resolving station across the configured cities."""
    feeds: List = []
    seen = set()
    for city in config.cities:
        spec = get_resolution(venue, city)
        if spec is None or spec.station in seen:
            continue
        seen.add(spec.station)
        if spec.region == "us":
            # US: Synoptic (fast, if token) + IEM (free near-real-time) + NWS (free QC).
            if config.synoptic_token:
                f = _make_feed("src.data.feeds.synoptic_obs", "SynopticObsFeed", spec.station, config)
                if f:
                    feeds.append(f)
            for module, cls in (("src.data.feeds.iem_obs", "IemObsFeed"),
                                ("src.data.feeds.nws_obs", "NwsObsFeed")):
                f = _make_feed(module, cls, spec.station, config)
                if f:
                    feeds.append(f)
        else:
            module, cls = _REGION_FEED_MODULE.get(spec.region, (None, None))
            if module:
                f = _make_feed(module, cls, spec.station, config)
                if f:
                    feeds.append(f)
    return feeds


# ---- market feeds --------------------------------------------------------

def build_market_feed(config: Config, venue: str):
    if venue == "kalshi":
        from src.data.feeds.kalshi_market_feed import KalshiMarketFeed
        return KalshiMarketFeed(config)
    if venue == "polymarket":
        from src.data.feeds.pm_market_feed import PolymarketMarketFeed
        return PolymarketMarketFeed(config)
    raise ValueError(f"unknown venue {venue}")


# ---- execution adapter ---------------------------------------------------

def build_execution_adapter(config: Config, venue: str) -> BaseAdapter:
    """SIM → SimAdapter; PAPER/LIVE → the venue adapter."""
    if config.mode == "sim":
        return SimAdapter(starting_cash=config.bankroll_usd)
    if venue == "kalshi":
        from src.execution.adapters.kalshi import KalshiAdapter
        return KalshiAdapter(config)
    if venue == "polymarket":
        from src.execution.adapters.polymarket import PolymarketAdapter
        return PolymarketAdapter(config)
    raise ValueError(f"unknown venue {venue}")


# ---- venues in play ------------------------------------------------------

def active_venues(config: Config) -> List[str]:
    venues = [config.default_venue]
    if config.polymarket_allowed() and "polymarket" not in venues:
        venues.append("polymarket")
    return venues


# ---- assemble the loop ---------------------------------------------------

def build_loop(config: Optional[Config] = None,
               strategy_names: Optional[Sequence[str]] = None) -> WeatherBotLoop:
    config = config or get_config()
    narrator = Narrator(config.log_dir)
    kill = KillSwitch(config.kill_switch_file)
    limits = RiskLimits.from_config(config)
    risk = RiskEngine(limits=limits, kill_switch=kill)
    router = ExecutionRouter(risk, event_log_path=config.log_dir / "order_events.jsonl")

    obs_tracker = ObsTracker()
    forecast_clients = build_forecast_clients(config)

    catalogs: Dict[str, EventCatalog] = {}
    obs_feeds: List = []
    for venue in active_venues(config):
        try:
            market_feed = build_market_feed(config, venue)
        except Exception as exc:
            log.warning("[factory] market feed for %s unavailable: %s", venue, exc)
            continue
        catalogs[venue] = EventCatalog(market_feed, obs_tracker, forecast_clients)
        obs_feeds.extend(build_obs_feeds(config, venue))
        # Register the execution adapter for this venue (or the shared sim adapter).
        try:
            adapter = build_execution_adapter(config, venue)
            router.register_adapter(adapter, venue=(venue if config.mode != "sim" else "sim"))
        except Exception as exc:
            log.warning("[factory] execution adapter for %s unavailable: %s", venue, exc)

    # De-dup obs feeds by (class, station).
    uniq, seen = [], set()
    for f in obs_feeds:
        key = (type(f).__name__, getattr(f, "station", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(f)

    strategies = build_strategies(strategy_names)
    narrator.note("factory assembled loop",
                  venues=list(catalogs), obs_feeds=[type(f).__name__ for f in uniq],
                  forecast=[c.name for c in forecast_clients],
                  adapters=list(router.registered()))
    return WeatherBotLoop(
        config=config, router=router, obs_tracker=obs_tracker,
        catalogs=catalogs, strategies=strategies, obs_feeds=uniq, narrator=narrator,
    )
