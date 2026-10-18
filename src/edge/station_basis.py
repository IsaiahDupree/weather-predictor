"""
Station basis — calibration-free cross-venue relative value for the SAME city.

Kalshi and Polymarket both run NYC daily-high markets, but they resolve on
*different* stations: Kalshi on KNYC (Central Park), Polymarket on KLGA
(LaGuardia). Both stations see essentially the same weather, so the only
structural difference between the two venues' fair prices is the basis

    delta = high(KLGA) - high(KNYC)

which is a *measured historical distribution*, not a forecast. That is what makes
this edge calibration-free: we never predict tomorrow's temperature. We take one
venue's market-implied distribution over its station's high, shift it by the
measured basis, and reprice the OTHER venue's buckets. Where a venue's quoted
price deviates from the other-venue-implied fair value by more than costs + basis
uncertainty, there is a relative-value dislocation (buy the cheap leg, sell the
rich leg; the shared weather risk cancels, the basis-noise risk does not).

All distributions here are discrete pmfs over integer °F: ``{temp: prob}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# How many integer degrees to spread an open-ended ("X or below/above") bucket's
# probability mass across when turning prices into a pmf.
_OPEN_TAIL_DEG = 8


@dataclass(frozen=True)
class BasisModel:
    """Empirical pmf of delta = high(B) - high(A), rounded to whole °F."""

    pmf: Dict[int, float]          # delta -> probability (sums to 1)
    mean: float
    std: float
    n: int                          # paired sample size

    def is_usable(self, min_n: int = 30) -> bool:
        return self.n >= min_n and bool(self.pmf)


def fit_basis(pairs: Sequence[Tuple[float, float]]) -> BasisModel:
    """Fit a BasisModel from (high_A, high_B) paired daily highs.

    delta = round(high_B - high_A). Returns an empty model if no pairs.
    """
    deltas = [round(b - a) for a, b in pairs]
    n = len(deltas)
    if n == 0:
        return BasisModel(pmf={}, mean=0.0, std=0.0, n=0)
    counts: Dict[int, int] = {}
    for d in deltas:
        counts[d] = counts.get(d, 0) + 1
    pmf = {d: c / n for d, c in counts.items()}
    mean = sum(deltas) / n
    var = sum((d - mean) ** 2 for d in deltas) / n
    return BasisModel(pmf=pmf, mean=round(mean, 3), std=round(var ** 0.5, 3), n=n)


@dataclass(frozen=True)
class ConditionalBasisModel:
    """Regime-dependent basis: a delta pmf per source-temperature bin.

    The KNYC/KLGA basis is NOT stationary — on hot days LaGuardia runs ~2°F
    hotter than Central Park, while on cool days they track within ~0.5°F. Using
    the unconditional basis manufactures false cross-venue edges, so we condition
    the delta distribution on the source station's own temperature level.
    """

    bins: List[Tuple[Optional[float], Optional[float], BasisModel]]  # (lo, hi, model)
    fallback: BasisModel
    condition_on: str  # "A" or "B" — which station's high the bins are keyed on

    def model_for(self, source_temp: float) -> BasisModel:
        for lo, hi, model in self.bins:
            if (lo is None or source_temp >= lo) and (hi is None or source_temp < hi):
                if model.n >= 5:           # enough samples in this regime
                    return model
        return self.fallback

    @property
    def n(self) -> int:
        return self.fallback.n


def fit_conditional_basis(
    pairs: Sequence[Tuple[float, float]], *, condition_on: str = "A",
    edges: Sequence[float] = (80.0, 85.0),
) -> ConditionalBasisModel:
    """Fit a regime-split basis. ``pairs`` are (high_A, high_B); ``condition_on``
    picks whether bins key off station A's or B's high. ``edges`` are the bin
    boundaries (e.g. (80,85) -> cool <80, mild 80-85, hot >=85)."""
    idx = 0 if condition_on == "A" else 1
    bounds = [None] + list(edges) + [None]
    bins: List[Tuple[Optional[float], Optional[float], BasisModel]] = []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        sel = [(a, b) for (a, b) in pairs
               if (lo is None or (a, b)[idx] >= lo) and (hi is None or (a, b)[idx] < hi)]
        bins.append((lo, hi, fit_basis(sel)))
    return ConditionalBasisModel(bins=bins, fallback=fit_basis(pairs), condition_on=condition_on)


def normalize(qmap: Dict[str, float]) -> Dict[str, float]:
    """Scale a bucket-probability map to sum to 1 (strips the book's overround)."""
    total = sum(v for v in qmap.values() if v and v > 0)
    if total <= 0:
        return {k: 0.0 for k in qmap}
    return {k: (max(0.0, v) / total) for k, v in qmap.items()}


def prices_to_pmf(buckets: Sequence[Tuple[Optional[float], Optional[float], float]]) -> Dict[int, float]:
    """Turn (lo, hi, q) buckets into a per-integer-degree pmf over the high.

    ``q`` should already be normalized across the buckets (use ``normalize`` on
    the bucket-prob map first). Each bucket's mass is spread uniformly over the
    integer degrees it covers (half-open [lo, hi)); open-ended buckets spread
    over a bounded tail of ``_OPEN_TAIL_DEG`` degrees.
    """
    pmf: Dict[int, float] = {}
    for lo, hi, q in buckets:
        if not q or q <= 0:
            continue
        degs = _bucket_degrees(lo, hi)
        if not degs:
            continue
        share = q / len(degs)
        for d in degs:
            pmf[d] = pmf.get(d, 0.0) + share
    return _renorm_pmf(pmf)


def _bucket_degrees(lo: Optional[float], hi: Optional[float]) -> List[int]:
    """Integer degrees a half-open [lo, hi) bucket covers (bounded for open ends)."""
    if lo is None and hi is None:
        return []
    if lo is None:                       # (-inf, hi): "hi-1 or below"
        top = int(round(hi)) - 1
        return list(range(top - _OPEN_TAIL_DEG + 1, top + 1))
    if hi is None:                       # [lo, +inf): "lo or higher"
        base = int(round(lo))
        return list(range(base, base + _OPEN_TAIL_DEG))
    a, b = int(round(lo)), int(round(hi))
    return list(range(a, b)) or [a]      # [lo, hi) integer degrees


def convolve(temp_pmf: Dict[int, float], basis: BasisModel, *, sign: int = 1) -> Dict[int, float]:
    """Shift a temp pmf by the basis. sign=+1: A->B (B=A+delta). sign=-1: B->A."""
    out: Dict[int, float] = {}
    for t, pt in temp_pmf.items():
        if pt <= 0:
            continue
        for d, pd in basis.pmf.items():
            shifted = t + sign * d
            out[shifted] = out.get(shifted, 0.0) + pt * pd
    return _renorm_pmf(out)


def convolve_conditional(
    temp_pmf: Dict[int, float], cbm: ConditionalBasisModel, *, sign: int = 1
) -> Dict[int, float]:
    """Like ``convolve`` but the basis pmf is chosen per source temperature.

    For each source temp t we use the delta distribution for t's regime bin, so a
    hot-day source mass gets the hot-day basis and a cool-day mass the cool basis.
    """
    out: Dict[int, float] = {}
    for t, pt in temp_pmf.items():
        if pt <= 0:
            continue
        model = cbm.model_for(t)
        for d, pd in model.pmf.items():
            shifted = t + sign * d
            out[shifted] = out.get(shifted, 0.0) + pt * pd
    return _renorm_pmf(out)


def pmf_to_bucket_probs(
    pmf: Dict[int, float], buckets: Sequence[Tuple[str, Optional[float], Optional[float]]]
) -> Dict[str, float]:
    """Integrate a per-degree pmf onto a (label, lo, hi) bucket grid -> {label: q}."""
    out: Dict[str, float] = {}
    for label, lo, hi in buckets:
        s = 0.0
        for t, p in pmf.items():
            if (lo is None or t >= lo) and (hi is None or t < hi):
                s += p
        out[label] = round(s, 6)
    return out


def _renorm_pmf(pmf: Dict[int, float]) -> Dict[int, float]:
    total = sum(pmf.values())
    if total <= 0:
        return {}
    return {t: p / total for t, p in pmf.items() if p > 0}
