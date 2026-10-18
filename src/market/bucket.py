"""
Bucket model for temperature prediction markets.

A daily-high market is a set of mutually-exclusive, collectively-exhaustive
binary buckets, e.g. Kalshi's "≤68°F", "69–70°F", "71–72°F", … "≥81°F".
Interior brackets are 2°F wide; the two edges are open-ended.

This module turns (a) a resolved temperature into the winning bucket, and (b) a
set of forecast samples (ensemble members) into a probability vector over
buckets — the ``q`` the tail-bucket sniper and intra-market arb consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class Bucket:
    """A single outcome bucket. Half-open interval [lo, hi); None = open end.

    label : venue outcome label (e.g. "71-72", "80 or above").
    lo    : inclusive lower bound (None = open below).
    hi    : exclusive upper bound (None = open above).
    """

    label: str
    lo: Optional[float]
    hi: Optional[float]

    def contains(self, temp: float) -> bool:
        if self.lo is not None and temp < self.lo:
            return False
        if self.hi is not None and temp >= self.hi:
            return False
        return True

    @property
    def midpoint(self) -> Optional[float]:
        if self.lo is not None and self.hi is not None:
            return (self.lo + self.hi) / 2.0
        return self.lo if self.lo is not None else self.hi


class BucketGrid:
    """An ordered set of mutually-exclusive buckets covering the real line."""

    def __init__(self, buckets: Sequence[Bucket]) -> None:
        if not buckets:
            raise ValueError("BucketGrid needs at least one bucket")
        self.buckets: List[Bucket] = list(buckets)

    def __len__(self) -> int:
        return len(self.buckets)

    def __iter__(self):
        return iter(self.buckets)

    def which(self, temp: float) -> Optional[Bucket]:
        """The bucket a resolved temperature falls into (None if uncovered)."""
        for b in self.buckets:
            if b.contains(temp):
                return b
        return None

    def probs_from_samples(self, samples: Sequence[float]) -> Dict[str, float]:
        """Empirical probability per bucket from forecast samples (ensemble Tmax).

        Returns {bucket_label: probability}. Samples outside every bucket are
        dropped from the denominator (shouldn't happen with open-ended edges).
        """
        counts: Dict[str, int] = {b.label: 0 for b in self.buckets}
        n = 0
        for s in samples:
            b = self.which(s)
            if b is not None:
                counts[b.label] += 1
                n += 1
        if n == 0:
            return {label: 0.0 for label in counts}
        return {label: c / n for label, c in counts.items()}

    @classmethod
    def kalshi_style(
        cls,
        lo_edge: float,
        hi_edge: float,
        width: float = 2.0,
    ) -> "BucketGrid":
        """Build a Kalshi-style grid: open-ended edges + ``width``-wide interior.

        e.g. kalshi_style(69, 81, 2) →
            "<=68", "69-70", "71-72", ..., "79-80", ">=81".
        ``lo_edge`` is the first interior lower bound; ``hi_edge`` the last
        interior upper bound (exclusive open-end start).
        """
        buckets: List[Bucket] = [Bucket(f"<={lo_edge - 1:g}", None, lo_edge)]
        x = lo_edge
        while x < hi_edge:
            top = x + width
            buckets.append(Bucket(f"{x:g}-{top - 1:g}", x, top))
            x = top
        buckets.append(Bucket(f">={hi_edge:g}", hi_edge, None))
        return cls(buckets)


def winning_bucket(grid: BucketGrid, resolved_temp: float) -> Optional[Bucket]:
    """Convenience: the bucket that resolves YES for a final temperature."""
    return grid.which(resolved_temp)
