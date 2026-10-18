"""
Pirate Weather forecast client (point daily-Tmax).

Pirate Weather is a drop-in, Dark-Sky-compatible REST API backed by NOAA's
operational models: HRRR (short range), NBM, and the GEFS ensemble / GFS for
longer leads. It returns a single blended point forecast per location/day, so
this client emits a ``point`` (no ensemble members).

Source:   https://docs.pirateweather.net/en/latest/API/
Endpoint: GET https://api.pirateweather.net/forecast/{apikey}/{lat},{lon}
Cadence:  Backing models refresh roughly hourly (HRRR/RTMA) out to ~hours, and
          every 6h (GFS/GEFS) for multi-day leads; poll no more than ~hourly.
Auth:     API key embedded in the URL path (free tier: 10k calls/month).

Response shape used here (per the docs):
  daily.data[] each carries:
    - "time"            : UNIX epoch seconds at LOCAL midnight of that day
    - "temperatureHigh" : daytime high (6:01am-6:00pm local), Fahrenheit when
                          units=us, Celsius when units=si/ca/uk
    - "temperatureMax"  : full-calendar-day maximum (fallback)

Units: request ``units=us`` for "F" (Fahrenheit) and ``units=si`` for "C"
(Celsius); the API returns native values in those units, so no conversion is
needed and ForecastResult.units matches the requested ``units``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult

_BASE_URL = "https://api.pirateweather.net/forecast"
_TIMEOUT = 10.0


class PirateForecast(ForecastClient):
    """Daily-Tmax point forecast from Pirate Weather (GEFS/HRRR-backed)."""

    name = "pirate"

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._config.pirate_weather_api_key)

    # ------------------------------------------------------------------

    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: _dt.date,
        units: str = "F",
    ) -> ForecastResult:
        """Return Pirate Weather's daily-max-temperature point for ``target_date``.

        ``units`` "F" maps to the API ``us`` query (Fahrenheit); anything else
        ("C") maps to ``si`` (Celsius). The returned ForecastResult.units
        matches the requested ``units``.
        """
        out_units = "C" if str(units).upper().startswith("C") else "F"
        api_units = "si" if out_units == "C" else "us"

        result = ForecastResult(
            source=self.name,
            target_date=target_date.isoformat(),
            units=out_units,
            members=[],
            point=None,
        )

        api_key = self._config.pirate_weather_api_key
        if not api_key:
            return result

        url = f"{_BASE_URL}/{api_key}/{lat},{lon}"
        params = {
            "units": api_units,
            # We only need the daily Tmax; drop everything else to save bytes.
            "exclude": "minutely,hourly,currently,alerts",
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()

        daily = payload.get("daily") or {}
        days = daily.get("data") or []
        if not isinstance(days, list):
            return result

        match = self._find_day(days, target_date)
        if match is None:
            return result

        high = self._read_high(match)
        if high is not None:
            result.point = high

        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _find_day(days: list, target_date: _dt.date) -> Optional[dict]:
        """Locate the daily entry whose local-midnight ``time`` falls on ``target_date``.

        Pirate Weather stamps each daily block at local midnight, so the UTC
        instant can land on the previous/next calendar day depending on the
        timezone offset. We compare the date in the day's own local frame by
        deriving the offset from the entry's ``time``; failing that, we fall
        back to a UTC-date match.
        """
        for entry in days:
            if not isinstance(entry, dict):
                continue
            t = entry.get("time")
            if t is None:
                continue
            try:
                ts = float(t)
            except (TypeError, ValueError):
                continue
            # The entry's "time" is local midnight; converting to a naive UTC
            # date and to a date offset by up to one day brackets the local day.
            utc_dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
            candidates = {
                utc_dt.date(),
                (utc_dt + _dt.timedelta(hours=12)).date(),
                (utc_dt - _dt.timedelta(hours=12)).date(),
            }
            if target_date in candidates:
                return entry
        return None

    @staticmethod
    def _read_high(entry: dict) -> Optional[float]:
        """Extract a usable daily-high value, skipping missing/-99 sentinels."""
        for key in ("temperatureHigh", "temperatureMax"):
            val = entry.get(key)
            if val is None:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            # Guard against null/sentinel placeholders used by some feeds.
            if num <= -99.0:
                continue
            return num
        return None
