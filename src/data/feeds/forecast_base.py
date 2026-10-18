"""
Forecast client contract.

Forecasts are request/response per (location, date), not a tick stream, so they
use their own small base rather than ``BaseFeed``. A client returns the daily
maximum temperature for a target date as either an **ensemble** of members (a
real distribution — preferred, from Open-Meteo Ensemble / GEFS) or a single
**point** value (a model "vote"). The edge layer turns members into a per-bucket
probability vector; point-only sources are blended as votes or given a sigma.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class ForecastResult:
    source: str
    target_date: str               # ISO date the forecast is for
    units: str = "F"               # "F" | "C" — the units of members/point
    members: List[float] = field(default_factory=list)   # ensemble daily-Tmax members
    point: Optional[float] = None  # single daily-Tmax (non-ensemble sources)
    lead_hours: Optional[float] = None

    @property
    def has_distribution(self) -> bool:
        return len(self.members) >= 2

    @property
    def best_estimate(self) -> Optional[float]:
        if self.members:
            return sum(self.members) / len(self.members)
        return self.point


class ForecastClient(abc.ABC):
    name: str = "forecast"

    def is_available(self) -> bool:
        return True

    @abc.abstractmethod
    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: date,
        units: str = "F",
    ) -> ForecastResult:
        """Return the daily-max-temperature forecast for ``target_date``.

        ``units`` is the desired output unit ("F" or "C"); the client must
        request/convert so ``ForecastResult.units`` matches.
        """

    def health(self) -> dict:
        return {"name": self.name, "available": self.is_available()}
