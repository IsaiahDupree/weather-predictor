"""
Oracle-lag / sensor arbitrage — the core weather edge.

Read the raw station feed and compute a high-confidence ``q_real`` for each
bucket *before* the official finalized value the market resolves on has caught
up. When the raw obs already (nearly) determines the resolved bucket but the
market price still lags, EV = q - p is wide and we take it.

The edge decays to zero the moment the oracle prints, so signals carry their obs
age and the router's STALE_SIGNAL gate drops them once too old. A volatility
gate guards against acting on a not-yet-locked outcome.

Edge source: latency between the physical sensor and the market's resolution
source; humans react late to micro-updates in niche markets.
"""

from __future__ import annotations

from typing import List

from src.edge.vol_gate import passes as vol_passes
from src.strategies.base_strategy import Signal, Strategy
from src.strategies.common import nowcast_q, project_high, signal_meta
from src.market.snapshot import MarketSnapshot


class OracleLag(Strategy):
    name = "oracle_lag"
    kelly_lambda = 0.5            # high-confidence q (obs-derived), gated by vol

    # tunables
    ev_threshold: float = 0.10    # require a wide gap (this is a data-advantage edge)
    max_obs_age_s: float = 90.0   # only act on fresh obs
    vol_k: float = 3.0
    max_price: float = 0.97       # leave the last ~3¢ to the near-expiry scalper

    def generate_signals(self, snapshot: MarketSnapshot) -> List[Signal]:
        out: List[Signal] = []
        if snapshot.obs_age_s is not None and snapshot.obs_age_s > self.max_obs_age_s:
            return out
        if snapshot.obs_temp is None and snapshot.daily_high_so_far is None:
            return out

        q_map = nowcast_q(snapshot)
        if not q_map:
            return out
        mu, sigma = project_high(snapshot)
        sigma = sigma if sigma is not None else snapshot.resid_sigma

        for quote in snapshot.quotes:
            q = q_map.get(quote.label, 0.0)
            p = quote.yes_ask
            if p <= 0 or p > self.max_price:
                continue
            ev = q - p
            if ev < self.ev_threshold:
                continue
            # Volatility gate: how far is the projected high past this bucket's
            # upper edge (the line it would have to cross to spill into the next
            # bucket up)? For the open-ended top bucket, margin is effectively ∞.
            high = snapshot.daily_high_so_far if snapshot.daily_high_so_far is not None else (mu or 0.0)
            margin = float("inf") if quote.bucket.hi is None else (quote.bucket.hi - high)
            gate_ok = vol_passes(margin, sigma, self.vol_k)
            meta = signal_meta(snapshot, quote, q=q, p=p, outcome="yes",
                               strategy=self.name, vol_gate_pass=gate_ok)
            out.append(Signal(
                side="BUY", outcome="yes", target_price=p,
                strength=min(1.0, ev / 0.30),
                reason=f"oracle-lag YES {quote.label}: q_real={q:.3f} vs p={p:.3f} "
                       f"(obs_age={snapshot.obs_age_s}s, vol_gate={'ok' if gate_ok else 'FAIL'})",
                metadata=meta,
            ))
        return out
