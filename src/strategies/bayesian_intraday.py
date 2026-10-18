"""
Bayesian intraday — probability in motion.

Start from a climatology/forecast prior per bucket and update it with the live
intraday nowcast as observations arrive, weighting the obs more as the climate
day progresses and the nowcast tightens (``horizon_obs_weight``). Trade when the
posterior q diverges from the market price p by more than the threshold.

This is the article's Chicago >90°F example generalized to a bucket grid: prior
0.35 → posterior 0.76 after the morning obs while the market still sits at 0.40.
Less aggressive than oracle-lag (it blends prior + obs rather than trusting a
near-locked obs), so it carries the day before the outcome is decided.

Edge source: the market updates on the coarse forecast; the bot updates
continuously on the live obs trajectory.
"""

from __future__ import annotations

from typing import List

from src.edge.bayesian import horizon_obs_weight, posterior_in_bucket
from src.strategies.base_strategy import Signal, Strategy
from src.strategies.common import project_high, signal_meta
from src.market.snapshot import MarketSnapshot


class BayesianIntraday(Strategy):
    name = "bayesian_intraday"
    kelly_lambda = 0.25

    ev_threshold: float = 0.07

    def generate_signals(self, snapshot: MarketSnapshot) -> List[Signal]:
        out: List[Signal] = []
        mu, sigma = project_high(snapshot)
        if mu is None:
            return out
        sigma = sigma if sigma is not None else snapshot.resid_sigma
        obs_w = horizon_obs_weight(snapshot.fraction_of_day_elapsed, sigma)
        fq = snapshot.forecast_q or {}

        for quote in snapshot.quotes:
            prior_q = fq.get(quote.label, None)
            if prior_q is None:
                continue
            q = posterior_in_bucket(prior_q, mu, sigma, quote.bucket.lo, quote.bucket.hi, obs_w)
            p = quote.yes_ask
            if p <= 0:
                continue
            ev = q - p
            if ev < self.ev_threshold:
                continue
            meta = signal_meta(snapshot, quote, q=q, p=p, outcome="yes",
                               strategy=self.name)
            meta["prior_q"] = prior_q
            meta["obs_weight"] = obs_w
            out.append(Signal(
                side="BUY", outcome="yes", target_price=p,
                strength=min(1.0, ev / 0.20),
                reason=f"bayes YES {quote.label}: prior={prior_q:.3f} → post={q:.3f} "
                       f"vs p={p:.3f} (obs_w={obs_w:.2f})",
                metadata=meta,
            ))
        return out
