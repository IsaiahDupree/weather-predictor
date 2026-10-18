"""
BaseFeed — async contract for every data source (weather obs, forecast, market
prices).

Most weather sources are polled REST endpoints, not websockets, so the contract
is built around ``fetch()`` (one-shot) plus a ``stream(interval)`` helper that
turns any ``fetch()`` into an async generator with backoff. Market feeds that do
offer a websocket override ``stream()`` directly.

Implementations push every result into the ``MarketDataBus`` so downstream
consumers read normalized ``ObsTick`` / ``PriceTick`` objects regardless of the
upstream payload shape.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from src.data.market_data_bus import MarketDataBus

log = logging.getLogger(__name__)


class BaseFeed(abc.ABC):
    """Abstract polled/streamed feed.

    Subclasses set ``name`` and implement ``fetch()`` (returns a list of ticks,
    possibly empty). The default ``stream()`` polls ``fetch()`` every
    ``interval`` seconds with exponential backoff on error, publishing each tick
    to the bus.
    """

    name: str = "base"

    def __init__(self, bus: Optional[MarketDataBus] = None) -> None:
        self.bus = bus
        self._last_fetch_at: float = 0.0
        self._last_ok: bool = False
        self._errors: int = 0
        self._consecutive_errors: int = 0

    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def fetch(self) -> List[Any]:
        """Fetch the current data, returning a list of ObsTick/PriceTick.

        Must raise on a hard error (network/auth) so ``stream()`` can back off;
        return ``[]`` when there is simply nothing new.
        """

    def is_available(self) -> bool:
        """True if the feed has the credentials/config it needs to run."""
        return True

    # ------------------------------------------------------------------

    async def poll_once(self) -> List[Any]:
        """Run a single fetch, publish results to the bus, track health."""
        self._last_fetch_at = time.time()
        try:
            ticks = await self.fetch()
            self._last_ok = True
            self._consecutive_errors = 0
            if self.bus is not None:
                for t in ticks:
                    self.bus.publish(t)
            return ticks
        except Exception as exc:
            self._last_ok = False
            self._errors += 1
            self._consecutive_errors += 1
            log.warning("[feed %s] fetch failed: %s", self.name, exc)
            raise

    async def stream(
        self,
        interval: float,
        stop: Optional[asyncio.Event] = None,
        max_backoff: float = 60.0,
    ) -> AsyncIterator[List[Any]]:
        """Poll ``fetch()`` forever (until ``stop``), yielding each batch.

        Errors back off exponentially up to ``max_backoff`` and do not kill the
        stream — a transient API outage must not take the whole bot down.
        """
        backoff = interval
        while stop is None or not stop.is_set():
            try:
                ticks = await self.poll_once()
                backoff = interval
                yield ticks
                await self._sleep(interval, stop)
            except Exception:
                await self._sleep(backoff, stop)
                backoff = min(backoff * 2, max_backoff)

    @staticmethod
    async def _sleep(seconds: float, stop: Optional[asyncio.Event]) -> None:
        if stop is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_fetch_at": self._last_fetch_at,
            "last_ok": self._last_ok,
            "errors": self._errors,
            "consecutive_errors": self._consecutive_errors,
        }
