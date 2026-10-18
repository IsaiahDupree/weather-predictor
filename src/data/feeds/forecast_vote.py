"""
Independent model-vote forecast client (Tomorrow.io + OpenWeatherMap).

Two commercial point-forecast models used to cross-check the ensemble sources.
Each provider returns a single daily-Tmax "vote"; we collect whichever points are
available into ``ForecastResult.members`` so that model *disagreement* is itself a
measurable signal, and set ``point`` to their mean.

Sources & cadence
------------------
* Tomorrow.io Weather Forecast (v4)
  - GET https://api.tomorrow.io/v4/weather/forecast
  - params: location="{lat},{lon}", timesteps=1d, units=imperial|metric, apikey=...
  - response: timelines.daily[] -> {time: ISO8601 (valid 08:00Z), values.temperatureMax}
  - docs: https://docs.tomorrow.io/reference/weather-forecast (verified 2026-05-30)
  - free tier refreshes roughly hourly; daily horizon out to ~5 days.

* OpenWeatherMap One Call API 3.0
  - GET https://api.openweathermap.org/data/3.0/onecall
  - params: lat, lon, exclude=current,minutely,hourly,alerts,
            units=imperial|metric, appid=...
  - response: daily[] -> {dt: unix-UTC seconds, temp.max}
  - docs: https://openweathermap.org/api/one-call-3 (verified 2026-05-30)
  - daily horizon out to 8 days; model updates several times per hour.

Units mapping: requested 'F' -> imperial, 'C' -> metric. Each provider returns
values already in the requested system, so no manual conversion is required.

The file imports cleanly with no credentials present; ``is_available()`` simply
returns False and ``daily_tmax`` yields an empty (members-less) result.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult

_TIMEOUT = httpx.Timeout(10.0)

# Sentinels that mean "no data" across the various providers.
_NULL_SENTINELS = (None, -99, -99.0, "-99")


def _units_param(units: str) -> str:
    """Map the contract's 'F'/'C' to the providers' imperial/metric system."""
    return "metric" if str(units).upper().startswith("C") else "imperial"


def _coerce_temp(value) -> Optional[float]:
    """Return a float temperature, or None for missing/sentinel/garbage values."""
    if value in _NULL_SENTINELS:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # Reject obvious missing-data sentinels.
    if f <= -90.0:
        return None
    return f


class VoteForecast(ForecastClient):
    """Cross-checking point forecasts from Tomorrow.io and OpenWeatherMap.

    Collects each independent model's daily-Tmax "vote" into a single
    :class:`ForecastResult` (members = available votes, point = their mean).
    """

    name = "vote"

    TOMORROW_URL = "https://api.tomorrow.io/v4/weather/forecast"
    OWM_URL = "https://api.openweathermap.org/data/3.0/onecall"

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(
            self.config.tomorrow_io_api_key or self.config.openweathermap_api_key
        )

    # ------------------------------------------------------------------

    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: date,
        units: str = "F",
    ) -> ForecastResult:
        """Gather independent daily-Tmax votes for ``target_date``.

        Each available provider contributes at most one member. Providers that
        are unconfigured or that error/return nothing are skipped silently so a
        single dead vendor never sinks the cross-check.
        """
        out_units = "C" if str(units).upper().startswith("C") else "F"
        system = _units_param(out_units)

        tasks = []
        if self.config.tomorrow_io_api_key:
            tasks.append(self._tomorrow_tmax(lat, lon, target_date, system))
        if self.config.openweathermap_api_key:
            tasks.append(self._owm_tmax(lat, lon, target_date, system))

        members: List[float] = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    continue
                if r is not None:
                    members.append(r)

        point = sum(members) / len(members) if members else None
        return ForecastResult(
            source=self.name,
            target_date=target_date.isoformat(),
            units=out_units,
            members=members,
            point=point,
        )

    # ------------------------------------------------------------------
    # Provider fetchers — each returns Optional[float] (one vote) or None.
    # ------------------------------------------------------------------

    async def _tomorrow_tmax(
        self, lat: float, lon: float, target_date: date, system: str
    ) -> Optional[float]:
        params = {
            "location": f"{lat},{lon}",
            "timesteps": "1d",
            "units": system,
            "apikey": self.config.tomorrow_io_api_key,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                self.TOMORROW_URL,
                params=params,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        daily = (data.get("timelines") or {}).get("daily") or []
        iso = target_date.isoformat()
        for entry in daily:
            t = entry.get("time")
            if not isinstance(t, str):
                continue
            # entry "time" is an ISO8601 instant valid at 08:00Z; match by date.
            if t[:10] != iso:
                continue
            values = entry.get("values") or {}
            return _coerce_temp(values.get("temperatureMax"))
        return None

    async def _owm_tmax(
        self, lat: float, lon: float, target_date: date, system: str
    ) -> Optional[float]:
        params = {
            "lat": lat,
            "lon": lon,
            "exclude": "current,minutely,hourly,alerts",
            "units": system,
            "appid": self.config.openweathermap_api_key,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                self.OWM_URL,
                params=params,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily") or []
        for entry in daily:
            dt = entry.get("dt")
            if dt in _NULL_SENTINELS:
                continue
            try:
                entry_date = datetime.fromtimestamp(
                    float(dt), tz=timezone.utc
                ).date()
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            if entry_date != target_date:
                continue
            temp = entry.get("temp") or {}
            return _coerce_temp(temp.get("max"))
        return None
