"""
MarketDataBus — normalized in-memory store + async pub/sub for the two data
kinds this bot consumes:

  * ``ObsTick``   — a raw surface observation (station temperature) from a
                    weather feed. This is the value the market resolves toward.
  * ``PriceTick`` — a market quote (best bid/ask + mid) for a binary outcome
                    token from a venue feed.

Feeds publish ticks; strategies/loop read the latest cached value synchronously
(weather is polled, so "latest" semantics matter more than streaming) and may
also subscribe to a topic queue for push delivery. The bus is the single point
where staleness is visible: every tick is timestamped and ``age()`` tells the
risk engine whether a signal is too old to act on.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ObsTick:
    """A single raw temperature observation for one station.

    ``temp`` is in the station's native units (``units``): US ASOS report °F,
    the Asian agencies report °C. Consumers compare against the market's
    ResolutionSpec.units and convert only when those differ.
    """
    station: str                 # station id (e.g. "KLGA", "466920")
    temp: float                  # observed air temperature in `units`
    units: str = "F"             # "F" | "C"
    timestamp: float = field(default_factory=time.time)   # our receipt time (epoch s)
    obs_time: Optional[float] = None    # the observation's own valid time (epoch s)
    source: str = ""             # feed name (synoptic | iem | nws | cwa | jma | kma | hko)
    qc: Optional[str] = None     # quality-control flag if the source provides one
    raw: Optional[str] = None    # raw METAR / payload fragment for audit

    @property
    def temp_f(self) -> float:
        """Temperature in °F regardless of native units (convenience for US logic)."""
        return self.temp if self.units == "F" else self.temp * 9.0 / 5.0 + 32.0

    @property
    def temp_c(self) -> float:
        return self.temp if self.units == "C" else (self.temp - 32.0) * 5.0 / 9.0

    @property
    def topic(self) -> str:
        return f"obs:{self.station}"

    def age(self, now: Optional[float] = None) -> float:
        """Seconds since the observation's valid time (falls back to receipt)."""
        ref = self.obs_time if self.obs_time is not None else self.timestamp
        return (now if now is not None else time.time()) - ref


@dataclass
class PriceTick:
    """A market quote for one binary outcome token."""
    venue: str                   # "kalshi" | "polymarket"
    symbol: str                  # ticker / clobTokenId
    bid: float = 0.0             # best YES bid (probability)
    ask: float = 0.0             # best YES ask (probability)
    bid_size: float = 0.0
    ask_size: float = 0.0
    timestamp: float = field(default_factory=time.time)
    raw: Optional[dict] = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ask or self.bid

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return max(0.0, self.ask - self.bid)
        return 0.0

    @property
    def topic(self) -> str:
        return f"price:{self.venue}:{self.symbol}"

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.timestamp


class MarketDataBus:
    """Latest-value cache + topic pub/sub for ObsTick and PriceTick."""

    def __init__(self, queue_max: int = 1000) -> None:
        self._latest: Dict[str, object] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._queue_max = queue_max

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, tick: object) -> None:
        """Cache the tick as the latest for its topic and fan out to subscribers."""
        topic = getattr(tick, "topic", None)
        if topic is None:
            raise TypeError(f"tick {tick!r} has no .topic")
        self._latest[topic] = tick
        for q in self._subscribers.get(topic, []):
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                # Drop the oldest, keep the newest — staleness is the enemy here.
                try:
                    q.get_nowait()
                    q.put_nowait(tick)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Read (latest-value)
    # ------------------------------------------------------------------

    def latest_obs(self, station: str) -> Optional[ObsTick]:
        v = self._latest.get(f"obs:{station}")
        return v if isinstance(v, ObsTick) else None

    def latest_price(self, venue: str, symbol: str) -> Optional[PriceTick]:
        v = self._latest.get(f"price:{venue}:{symbol}")
        return v if isinstance(v, PriceTick) else None

    def latest(self, topic: str) -> Optional[object]:
        return self._latest.get(topic)

    def all_obs(self) -> List[ObsTick]:
        return [v for v in self._latest.values() if isinstance(v, ObsTick)]

    # ------------------------------------------------------------------
    # Subscribe (push)
    # ------------------------------------------------------------------

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers[topic].append(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        if q in self._subscribers.get(topic, []):
            self._subscribers[topic].remove(q)
