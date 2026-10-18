"""
Intraday nowcast — project the current temperature forward to estimate the
remaining probability of crossing a threshold by resolution.

Used by the Bayesian-intraday and oracle-lag strategies. Deliberately simple and
physically grounded: a damped linear trend for the mean, with forecast
uncertainty that grows with the horizon. The output feeds ``bayesian.posterior``.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple


def normal_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def project(
    t_now: float,
    trend_per_hr: float,
    hours_remaining: float,
    resid_sigma_per_sqrt_hr: float = 1.5,
    damping: float = 0.8,
    max_temp: Optional[float] = None,
) -> Tuple[float, float]:
    """Project temperature ``hours_remaining`` ahead.

    mu    = t_now + trend_per_hr * hours * damping   (capped at max_temp)
    sigma = resid_sigma_per_sqrt_hr * sqrt(hours)    (uncertainty grows w/ horizon)

    ``damping`` < 1 reflects that an instantaneous warming rate doesn't persist
    linearly (afternoon highs plateau). ``max_temp`` lets the caller cap at a
    climatological record. ``hours_remaining`` <= 0 returns (t_now, small sigma).
    """
    h = max(0.0, hours_remaining)
    mu = t_now + trend_per_hr * h * damping
    if max_temp is not None:
        mu = min(mu, max_temp)
    sigma = max(0.1, resid_sigma_per_sqrt_hr * math.sqrt(max(h, 0.0)))
    return mu, sigma


def prob_exceeds(mu: float, sigma: float, threshold: float) -> float:
    """P(value >= threshold) for a Normal(mu, sigma)."""
    if sigma <= 0:
        return 1.0 if mu >= threshold else 0.0
    return 1.0 - normal_cdf((threshold - mu) / sigma)


def prob_in_interval(mu: float, sigma: float, lo: Optional[float], hi: Optional[float]) -> float:
    """P(lo <= value < hi) for a Normal(mu, sigma); None bounds = open-ended."""
    if sigma <= 0:
        v = mu
        if lo is not None and v < lo:
            return 0.0
        if hi is not None and v >= hi:
            return 0.0
        return 1.0
    p_lo = 0.0 if lo is None else normal_cdf((lo - mu) / sigma)
    p_hi = 1.0 if hi is None else normal_cdf((hi - mu) / sigma)
    return max(0.0, p_hi - p_lo)
