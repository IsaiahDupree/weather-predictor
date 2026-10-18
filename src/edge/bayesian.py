"""
Bayesian updating — probability in motion.

A static estimate is fine for one moment, but data keeps arriving. We start from
a climatology prior and update it with the intraday nowcast as observations come
in. The article's worked example (Chicago >90°F: prior 0.35 → posterior 0.76
after the morning obs) is exactly this.

Two entry points:
  * ``bayes_update`` — the literal P(A|B) = P(B|A)P(A)/P(B) formula.
  * ``posterior`` — the practical precision/horizon-weighted blend of the
    climatology prior and the nowcast-implied probability, which is what the
    intraday strategy actually calls each tick.
"""

from __future__ import annotations

from src.edge.nowcast import prob_exceeds, prob_in_interval
from typing import Optional


def bayes_update(prior: float, likelihood: float, marginal: float) -> float:
    """P(A|B) = P(B|A) * P(A) / P(B). Clamped to [0, 1]."""
    if marginal <= 0:
        return prior
    return max(0.0, min(1.0, likelihood * prior / marginal))


def posterior_exceeds(
    prior_q: float,
    mu: float,
    sigma: float,
    threshold: float,
    obs_weight: float,
) -> float:
    """Posterior P(value >= threshold), blending prior with the nowcast.

    ``obs_weight`` in [0,1] is how much to trust the intraday nowcast vs the
    climatology prior — it should rise through the day (early morning → trust
    climatology; mid-afternoon with the high nearly in → trust the nowcast).
    The runtime loop derives it from the fraction of the climate day elapsed
    and the nowcast precision.
    """
    q_obs = prob_exceeds(mu, sigma, threshold)
    w = max(0.0, min(1.0, obs_weight))
    return w * q_obs + (1.0 - w) * prior_q


def posterior_in_bucket(
    prior_q: float,
    mu: float,
    sigma: float,
    lo: Optional[float],
    hi: Optional[float],
    obs_weight: float,
) -> float:
    """Posterior P(lo <= value < hi), blending bucket prior with the nowcast."""
    q_obs = prob_in_interval(mu, sigma, lo, hi)
    w = max(0.0, min(1.0, obs_weight))
    return w * q_obs + (1.0 - w) * prior_q


def horizon_obs_weight(fraction_of_day_elapsed: float, sigma: float, max_sigma: float = 10.0) -> float:
    """Derive a sensible obs_weight from how much of the climate day is gone and
    how tight the nowcast is.

    More of the day elapsed → trust the obs more (the high is increasingly
    locked in). Tighter nowcast sigma → trust the obs more. Bounded to [0,1].
    """
    time_w = max(0.0, min(1.0, fraction_of_day_elapsed))
    precision_w = 1.0 - max(0.0, min(1.0, sigma / max_sigma)) if max_sigma > 0 else 0.5
    # Geometric mean keeps it conservative: both signals must agree to push high.
    return (time_w * precision_w) ** 0.5
