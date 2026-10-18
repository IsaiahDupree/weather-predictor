"""
Visual Crossing Timeline forecast/history client (point daily-Tmax).

The Visual Crossing **Timeline Weather API** is a single endpoint that
seamlessly stitches together historical observations, current conditions, and
multi-day forecasts for any location/date range. Because one call can return
either a past (observed) day or a future (forecast) day, this client is used
both for live forecasting and for backtesting: requesting a *past* date yields
the recorded observation, and requesting a *future* date yields the model
forecast. Each day carries a ``source`` flag telling you which it is.

Source:   https://www.visualcrossing.com/resources/documentation/weather-api/timeline-weather-api/
Endpoint: GET https://weather.visualcrossing.com/VisualCrossingWebServices/
              rest/services/timeline/{lat},{lon}/{date}
          ?key=...&unitGroup={us|metric}&include=days&elements=datetime,tempmax,source
Cadence:  Forecasts refresh several times daily (sub-hourly to hourly for the
          backing models); history is static once recorded. Poll no more than
          ~hourly. Free tier: 1000 records/day.
Auth:     API key passed as the ``key`` query parameter.

Response shape (per the docs):
  Top-level object -> ``days`` (array). Each day carries:
    - "datetime" : ISO local date string (YYYY-MM-DD) for that day
    - "tempmax"  : daily maximum temperature, in the requested unitGroup
                   (Fahrenheit for ``us``, Celsius for ``metric``)
    - "source"   : provenance flag. Per the docs the values are:
                   "obs"      historical observation,
                   "fcst"     forecast,
                   "histfcst" historical forecast,
                   "stats"    statistical (climatological) estimate,
                   "comb"     a combination within the same day.

Units: ``unitGroup=us`` returns Fahrenheit, ``unitGroup=metric`` returns
Celsius, so no conversion is required and ForecastResult.units matches the
requested ``units``. The VC ``source`` flag is stashed in
``ForecastResult.source`` (e.g. "visualcrossing:obs") so downstream consumers
can tell an observation apart from a true forecast.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult

_BASE_URL = (
    "https://weather.visualcrossing.com/VisualCrossingWebServices"
    "/rest/services/timeline"
)
_TIMEOUT = 10.0

# Source flags the Timeline API may return on a day record.
_VALID_SOURCES = {"obs", "fcst", "histfcst", "stats", "comb"}


class VisualCrossingClient(ForecastClient):
    """Daily-Tmax point from Visual Crossing Timeline (history + forecast)."""

    name = "visualcrossing"

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._config.visualcrossing_api_key)

    # ------------------------------------------------------------------

    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: _dt.date,
        units: str = "F",
    ) -> ForecastResult:
        """Return the daily-max-temperature point for ``target_date``.

        ``units`` "F" maps to the API ``unitGroup=us`` (Fahrenheit); anything
        else ("C") maps to ``unitGroup=metric`` (Celsius). The returned
        ForecastResult.units matches the requested ``units``. The Visual
        Crossing per-day ``source`` flag (obs/fcst/histfcst/stats/comb) is
        appended to ``ForecastResult.source`` as ``visualcrossing:<flag>``.
        """
        out_units = "C" if str(units).upper().startswith("C") else "F"
        unit_group = "metric" if out_units == "C" else "us"

        result = ForecastResult(
            source=self.name,
            target_date=target_date.isoformat(),
            units=out_units,
            members=[],
            point=None,
        )

        api_key = self._config.visualcrossing_api_key
        if not api_key:
            return result

        # The location and date are part of the path; encode them defensively.
        location = f"{lat},{lon}"
        url = f"{_BASE_URL}/{location}/{target_date.isoformat()}"
        params = {
            "key": api_key,
            "unitGroup": unit_group,
            "include": "days",
            "elements": "datetime,tempmax,source",
            "contentType": "json",
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()

        if not isinstance(payload, dict):
            return result

        days = payload.get("days") or []
        if not isinstance(days, list) or not days:
            return result

        day = self._select_day(days, target_date)
        if day is None:
            return result

        tmax = self._read_tempmax(day)
        if tmax is not None:
            result.point = tmax

        flag = self._read_source(day)
        if flag:
            # Stash the provenance flag so consumers can distinguish an
            # observation (backtest truth) from a forward forecast.
            result.source = f"{self.name}:{flag}"

        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _select_day(days: list, target_date: _dt.date) -> Optional[dict]:
        """Pick the day record matching ``target_date`` (else the first one).

        A single-date Timeline request returns exactly one day, but we still
        match on ``datetime`` to be robust if the API ever returns a window.
        """
        iso = target_date.isoformat()
        first: Optional[dict] = None
        for entry in days:
            if not isinstance(entry, dict):
                continue
            if first is None:
                first = entry
            dt_str = entry.get("datetime")
            if isinstance(dt_str, str) and dt_str[:10] == iso:
                return entry
        return first

    @staticmethod
    def _read_tempmax(day: dict) -> Optional[float]:
        """Extract a usable ``tempmax``, skipping null/sentinel placeholders."""
        val = day.get("tempmax")
        if val is None:
            return None
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        # Visual Crossing uses null for genuinely missing data; some upstream
        # feeds use -99 sentinels — treat those as missing too.
        if num <= -99.0:
            return None
        return num

    @staticmethod
    def _read_source(day: dict) -> Optional[str]:
        """Return the normalized per-day ``source`` flag, if recognized."""
        raw = day.get("source")
        if not isinstance(raw, str):
            return None
        flag = raw.strip().lower()
        if not flag:
            return None
        # Pass through known flags; for anything unexpected, keep the raw token
        # rather than dropping provenance entirely.
        return flag if flag in _VALID_SOURCES else flag
