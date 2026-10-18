"""
Forecast distribution → per-bucket probability vector ``q[]``.

The tail-bucket sniper and intra-market arb need a probability *distribution*
over the resolved daily high, not a single number. Two sources:

  * **Ensemble members** (Open-Meteo Ensemble / Pirate GEFS) — empirical: bin the
    members' daily-Tmax onto the bucket grid.
  * **Point + sigma** (a single forecast plus an uncertainty estimate) — analytic:
    integrate a Normal over each bucket.

Both return ``{bucket_label: q}`` summing to ~1. A light calibration shrink pulls
extreme tails toward a floor so a single ensemble outlier can't imply q≈0/1.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

from src.edge.nowcast import prob_in_interval
from src.market.bucket import BucketGrid


def ensemble_to_bucket_probs(
    members: Sequence[float],
    grid: BucketGrid,
    floor: float = 0.0,
) -> Dict[str, float]:
    """Empirical bucket probabilities from ensemble member daily-Tmax values."""
    probs = grid.probs_from_samples(members)
    if floor > 0:
        probs = _apply_floor(probs, floor)
    return probs


def normal_to_bucket_probs(
    mu: float,
    sigma: float,
    grid: BucketGrid,
    floor: float = 0.0,
) -> Dict[str, float]:
    """Analytic bucket probabilities by integrating Normal(mu, sigma) per bucket."""
    raw = {b.label: prob_in_interval(mu, sigma, b.lo, b.hi) for b in grid}
    total = sum(raw.values())
    if total > 0:
        raw = {k: v / total for k, v in raw.items()}
    if floor > 0:
        raw = _apply_floor(raw, floor)
    return raw


def blend(
    a: Dict[str, float],
    b: Dict[str, float],
    weight_a: float = 0.5,
) -> Dict[str, float]:
    """Convex blend of two bucket-probability vectors (model averaging / votes)."""
    keys = set(a) | set(b)
    wa = max(0.0, min(1.0, weight_a))
    out = {k: wa * a.get(k, 0.0) + (1 - wa) * b.get(k, 0.0) for k in keys}
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else out


def _apply_floor(probs: Dict[str, float], floor: float) -> Dict[str, float]:
    """Clamp each prob to >= floor, then renormalize (anti-overconfidence)."""
    clamped = {k: max(v, floor) for k, v in probs.items()}
    total = sum(clamped.values())
    return {k: v / total for k, v in clamped.items()} if total > 0 else clamped


def high_market_bucket_probs(
    daily_high_so_far: Optional[float],
    mu: float,
    sigma: float,
    grid: BucketGrid,
    floor: float = 0.0,
) -> Dict[str, float]:
    """Per-bucket q for a *daily-high* market, respecting monotonicity.

    The resolved value is the day's MAXIMUM, which can only rise. So:
      * a bucket whose entire range is below the high-so-far is dead (q = 0) —
        the high has already surpassed it,
      * the bucket containing the high-so-far, and buckets above it, get the
        probability mass from the projected remaining rise (``mu`` is the
        projected final high, ``sigma`` its uncertainty).

    Without a high-so-far we fall back to a plain Normal over the grid.
    """
    if daily_high_so_far is None:
        return normal_to_bucket_probs(mu, sigma, grid, floor=floor)

    # The projected final high is at least the high already observed.
    eff_mu = max(mu, daily_high_so_far)
    raw: Dict[str, float] = {}
    for b in grid:
        if b.hi is not None and b.hi <= daily_high_so_far:
            raw[b.label] = 0.0          # already surpassed — cannot win
            continue
        lo = b.lo if b.lo is None else max(b.lo, daily_high_so_far)
        raw[b.label] = prob_in_interval(eff_mu, sigma, lo, b.hi)
    total = sum(raw.values())
    if total > 0:
        raw = {k: v / total for k, v in raw.items()}
    if floor > 0:
        raw = _apply_floor(raw, floor)
    return raw


def disagreement(votes: Sequence[Dict[str, float]]) -> float:
    """Max total-variation distance between any pair of model vote vectors.

    A high value flags a bucket the models disagree on — i.e. a possibly
    mispriced market worth a closer look (or worth skipping for safety).
    """
    if len(votes) < 2:
        return 0.0
    keys = set().union(*[set(v) for v in votes])
    worst = 0.0
    for i in range(len(votes)):
        for j in range(i + 1, len(votes)):
            tv = 0.5 * sum(abs(votes[i].get(k, 0.0) - votes[j].get(k, 0.0)) for k in keys)
            worst = max(worst, tv)
    return worst
