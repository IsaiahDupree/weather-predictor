"""
WeatherBotLoop — the main scan → edge → size → risk → submit cycle.

Each cycle:
  1. Poll the obs feeds and ingest the latest station readings into the tracker.
  2. For each (venue, city), ask the catalog for MarketSnapshots.
  3. Run every strategy on each snapshot to produce Signals.
  4. Size each signal (fee-aware EV check + fractional-Kelly / fixed-clip), build
     a unified Order, and submit it through the risk-gated router with the gate
     context (vol-gate result, signal age, arbitrage flag, book depth).

Strategies decide *what*; this loop decides *how much* and enforces *whether* via
the router/RiskEngine. In SIM mode orders fill against the SimAdapter (real
public market data, simulated fills + $1/$0 settlement); PAPER/LIVE route to the
venue adapter.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Dict, List, Optional, Sequence, Tuple

from src.config import Config
from src.data.obs_tracker import ObsTracker
from src.edge.kelly import size_position
from src.execution.order import Mode, Order, Outcome, Side, TimeInForce
from src.execution.router import ExecutionRouter
from src.market.event_catalog import EventCatalog
from src.market.fees import fee_per_contract
from src.market.snapshot import BucketQuote, MarketSnapshot
from src.runtime.narrator import Narrator
from src.strategies.base_strategy import Signal, Strategy

log = logging.getLogger(__name__)


class WeatherBotLoop:
    def __init__(
        self,
        config: Config,
        router: ExecutionRouter,
        obs_tracker: ObsTracker,
        catalogs: Dict[str, EventCatalog],          # venue -> catalog
        strategies: Sequence[Strategy],
        obs_feeds: Sequence[object],                # objects with async fetch() -> [ObsTick]
        *,
        narrator: Optional[Narrator] = None,
        scan_interval_s: float = 60.0,
        obs_interval_s: float = 60.0,
    ) -> None:
        self.config = config
        self.router = router
        self.obs_tracker = obs_tracker
        self.catalogs = catalogs
        self.strategies = list(strategies)
        self.obs_feeds = list(obs_feeds)
        self.narrator = narrator or Narrator(config.log_dir)
        self.scan_interval_s = scan_interval_s
        self.obs_interval_s = obs_interval_s
        self._stop = asyncio.Event()
        self.base_clip = max(1.0, config.bankroll_usd * 0.002)   # ~$2 on $1000
        # counters
        self.cycles = 0
        self.signals_seen = 0
        self.orders_submitted = 0
        self.orders_filled = 0
        self.orders_rejected = 0

    # ------------------------------------------------------------------

    async def poll_obs_once(self) -> int:
        n = 0
        for feed in self.obs_feeds:
            try:
                ticks = await feed.fetch()
            except Exception as exc:
                log.debug("[loop] obs feed %s failed: %s", getattr(feed, "name", "?"), exc)
                continue
            for t in ticks or []:
                self.obs_tracker.ingest(t)
                n += 1
        return n

    async def scan_once(self) -> None:
        for city in self.config.cities:
            for venue, catalog in self.catalogs.items():
                snaps = await catalog.snapshots_for_city(city)
                for snap in snaps:
                    await self._evaluate_snapshot(snap)

    async def _evaluate_snapshot(self, snap: MarketSnapshot) -> None:
        for strat in self.strategies:
            try:
                signals = strat.generate_signals(snap)
            except Exception:
                log.exception("[loop] strategy %s raised", strat.name)
                continue
            for sig in signals:
                if not sig.is_actionable:
                    continue
                self.signals_seen += 1
                self.narrator.signal(snap.venue, strat.name, sig.reason, sig.metadata)
                await self._handle_signal(snap, strat, sig)

    # ------------------------------------------------------------------

    async def _handle_signal(self, snap: MarketSnapshot, strat: Strategy, sig: Signal) -> None:
        quote = snap.quote_for(sig.metadata.get("bucket_label", ""))
        if quote is None:
            return
        p = float(sig.target_price or 0.0)
        if not (0.0 < p < 1.0):
            self.narrator.skip(quote.symbol, "no valid price")
            return

        q = float(sig.metadata.get("q", 0.0))
        win_prob = q if sig.outcome == Outcome.YES else (1.0 - q)
        is_arb = bool(sig.metadata.get("is_arbitrage"))

        # Fee-aware EV check (skip arbitrage — it is risk-free by construction).
        fee_pc = fee_per_contract(snap.venue, p)
        net_ev = (win_prob - p) - fee_pc
        if not is_arb and net_ev <= self.config.min_ev_threshold:
            self.narrator.skip(quote.symbol, f"net EV {net_ev:.3f} below threshold after fees")
            return

        # Book depth on the side we're buying (cap order to a fraction of it).
        depth = self._ask_depth(quote, sig.outcome)
        liq_cap_notional = self.config.liquidity_depth_frac * depth * p if depth > 0 else self.config.max_order_notional

        notional = size_position(
            q=win_prob, price=p, bankroll=self.config.bankroll_usd,
            lam=strat.kelly_lambda, base_clip=self.base_clip,
            ev_threshold=(0.0 if is_arb else self.config.min_ev_threshold),
            max_order_notional=self.config.max_order_notional,
            liquidity_cap_notional=liq_cap_notional,
        )
        if notional <= 0:
            self.narrator.skip(quote.symbol, "sized to zero")
            return

        qty = notional / p
        if snap.venue == "kalshi":
            qty = float(math.floor(qty))     # Kalshi trades whole contracts
        if qty < 1:
            self.narrator.skip(quote.symbol, f"sized below 1 contract (notional ${notional:.2f})")
            return

        order = Order(
            symbol=quote.symbol,
            side=Side.BUY,
            qty=qty,
            outcome=sig.outcome,
            limit_price=p,
            time_in_force=(TimeInForce.IOC if (is_arb or strat.name == "near_expiry_scalp") else TimeInForce.GTC),
            broker=snap.venue,
            mode=Mode(self.config.mode),
            metadata={
                "event_ticker": snap.event_ticker,
                "bucket_label": quote.label,
                "strategy": strat.name,
                "token_id_no": quote.token_id_no,
                "q": q, "p": p, "net_ev": net_ev,
                "city": snap.city,
            },
        )

        signal_age = sig.metadata.get("obs_age_s") if strat.name in ("oracle_lag", "bayesian_intraday") else None
        submitted = await self.router.submit(
            order, ref_price=p,
            event_ticker=snap.event_ticker,
            resolution_known=snap.resolution_known(),
            signal_age_s=signal_age,
            vol_gate_pass=sig.metadata.get("vol_gate_pass", True),
            book_depth=depth or None,
            is_arbitrage=is_arb,
        )
        self.orders_submitted += 1
        status = submitted.status.value
        if status == "filled":
            self.orders_filled += 1
        elif status == "rejected":
            self.orders_rejected += 1
        self.narrator.order("BUY", order.symbol, sig.outcome.value, qty, p, status,
                            detail=submitted.error or submitted.metadata.get("risk_code"))

    @staticmethod
    def _ask_depth(quote: BucketQuote, outcome: Outcome) -> float:
        levels = quote.yes_ask_levels if outcome == Outcome.YES else quote.no_ask_levels
        if levels:
            return sum(sz for _, sz in levels)
        return quote.yes_ask_size if outcome == Outcome.YES else quote.no_ask_size

    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.config.assert_runnable()
        self.narrator.note("loop starting", mode=self.config.mode,
                           venues=list(self.catalogs), cities=self.config.cities,
                           strategies=[s.name for s in self.strategies])
        last_obs = 0.0
        while not self._stop.is_set():
            cycle_start = time.time()
            if cycle_start - last_obs >= self.obs_interval_s:
                got = await self.poll_obs_once()
                last_obs = cycle_start
                log.debug("[loop] ingested %d obs", got)
            try:
                await self.scan_once()
            except Exception:
                log.exception("[loop] scan cycle failed")
            self.cycles += 1
            await self._sleep(self.scan_interval_s)

    async def run_once(self) -> Dict[str, int]:
        """Single poll+scan cycle — used by tests and one-shot runs."""
        await self.poll_obs_once()
        await self.scan_once()
        self.cycles += 1
        return self.stats()

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stats(self) -> Dict[str, int]:
        return {
            "cycles": self.cycles,
            "signals_seen": self.signals_seen,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_rejected": self.orders_rejected,
        }
