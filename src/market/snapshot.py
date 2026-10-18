"""
MarketSnapshot — the immutable view of one temperature event that the runtime
loop assembles each tick and hands to every strategy.

A temperature *event* (e.g. "NYC daily high on 2026-05-30") is a set of
mutually-exclusive *buckets*, each a binary YES/NO market. The snapshot bundles,
for that event:

  * the resolution spec (which station, which climate day, units),
  * the bucket grid + current quotes (YES/NO bid/ask + ask-side book depth),
  * the forecast probability per bucket (``forecast_q``),
  * the latest raw observation (``obs_temp``) and the daily high so far,
  * time context (seconds to resolution, fraction of climate day elapsed),
  * the per-contract fee for the venue.

Strategies are pure functions of this object; they never touch the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.market.bucket import Bucket, BucketGrid
from src.market.resolution import ResolutionSpec


@dataclass
class BucketQuote:
    """Current market quotes + book for one bucket's binary market."""

    bucket: Bucket
    symbol: str                              # venue instrument id (Kalshi ticker / PM YES clobTokenId)
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    yes_ask_levels: List[Tuple[float, float]] = field(default_factory=list)  # ascending (price,size)
    no_ask_levels: List[Tuple[float, float]] = field(default_factory=list)
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    no_bid_size: float = 0.0
    no_ask_size: float = 0.0
    token_id_no: Optional[str] = None        # PM NO token id (None for Kalshi)

    @property
    def yes_mid(self) -> float:
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_ask or self.yes_bid

    @property
    def label(self) -> str:
        return self.bucket.label


@dataclass
class MarketSnapshot:
    venue: str
    city: str
    event_ticker: str
    resolution: ResolutionSpec
    grid: BucketGrid
    quotes: List[BucketQuote]
    forecast_q: Dict[str, float] = field(default_factory=dict)   # bucket_label -> forecast probability
    obs_temp: Optional[float] = None         # latest raw observation (station units)
    obs_age_s: Optional[float] = None
    daily_high_so_far: Optional[float] = None
    seconds_to_resolution: float = 0.0
    fraction_of_day_elapsed: float = 0.0
    fee_per_contract: float = 0.0
    trend_per_hr: Optional[float] = None     # observed warming rate (for nowcast)
    resid_sigma: float = 1.5                 # residual obs volatility per sqrt-hour
    ts: float = field(default_factory=time.time)

    # ------------------------------------------------------------------

    def quote_for(self, label: str) -> Optional[BucketQuote]:
        for q in self.quotes:
            if q.label == label:
                return q
        return None

    @property
    def hours_to_resolution(self) -> float:
        return max(0.0, self.seconds_to_resolution / 3600.0)

    def threshold_below(self, bucket: Bucket) -> Optional[float]:
        """The lower edge of a bucket — the temp the high must reach to enter it."""
        return bucket.lo

    def resolution_known(self) -> bool:
        return self.resolution is not None
