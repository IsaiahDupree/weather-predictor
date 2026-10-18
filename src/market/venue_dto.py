"""
Venue-neutral market DTOs + the MarketFeed contract.

A ``MarketFeed`` discovers the open temperature events on a venue (Kalshi or
Polymarket) and reads their current quotes/order books, returning venue-neutral
objects so the event catalog and the rest of the bot never branch on venue.
``discover()`` maps a city to its open events + buckets + instrument ids;
``quotes()`` reads the live YES/NO prices and ask-side depth for one market.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional

from src.market.bucket import Bucket
from src.market.snapshot import BucketQuote


@dataclass
class RawBucket:
    """One bucket of a temperature event as the venue exposes it."""
    label: str
    lo: Optional[float]           # inclusive lower bound (°, market units); None = open below
    hi: Optional[float]           # exclusive upper bound; None = open above
    yes_symbol: str               # Kalshi market ticker / Polymarket YES clobTokenId
    no_token_id: Optional[str] = None   # Polymarket NO clobTokenId (Kalshi: None)

    def to_bucket(self) -> Bucket:
        return Bucket(label=self.label, lo=self.lo, hi=self.hi)


@dataclass
class RawMarket:
    """One temperature event (a set of mutually-exclusive buckets)."""
    venue: str
    city: str
    event_ticker: str
    close_time: float             # resolution/expiry time (epoch seconds, UTC)
    buckets: List[RawBucket] = field(default_factory=list)
    units: str = "F"              # market temperature units
    raw: Optional[dict] = None


class MarketFeed(abc.ABC):
    name: str = "market"
    venue: str = "venue"

    def is_available(self) -> bool:
        return True

    @abc.abstractmethod
    async def discover(self, city: str) -> List[RawMarket]:
        """Return the currently-open daily-temperature events for ``city``."""

    @abc.abstractmethod
    async def quotes(self, market: RawMarket) -> List[BucketQuote]:
        """Return live BucketQuotes (YES/NO bid/ask + ask-side depth) for a market."""

    def health(self) -> dict:
        return {"name": self.name, "venue": self.venue, "available": self.is_available()}
