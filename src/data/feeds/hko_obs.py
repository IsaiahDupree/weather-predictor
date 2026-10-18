"""
HkoObsFeed — Hong Kong Observatory current weather observations (rhrread).

Source : Hong Kong Observatory Open Data API ("weatherAPI" / Regional Weather
         in Hong Kong, dataType=rhrread).
Docs   : https://www.hko.gov.hk/en/abouthko/opendata_intro.htm
         https://data.weather.gov.hk/weatherAPI/doc/HKO_Open_Data_API_Documentation.pdf
Endpoint (verified 2026-05-30 against a live response):
         GET https://data.weather.gov.hk/weatherAPI/opendata/weather.php
             ?dataType=rhrread&lang=en
Auth   : none — fully open, no API key.
Cadence: HKO refreshes ``rhrread`` roughly every 10 minutes; poll at ~5-10 min.

Response shape (relevant slice)::

    {
      "temperature": {
        "recordTime": "2026-05-31T07:00:00+08:00",
        "data": [
          {"place": "Hong Kong Observatory", "value": 28, "unit": "C"},
          {"place": "King's Park",            "value": 29, "unit": "C"},
          ...
        ]
      },
      ...
    }

We pull the entry whose ``place`` matches the requested station (default the
official airport-grade site "Hong Kong Observatory"), report it in native
Celsius, and stamp it with the observation's own ``recordTime`` (HKT, +08:00).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, List, Optional

import httpx

from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_ENDPOINT = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
_TIMEOUT = 10.0
_DEFAULT_PLACE = "Hong Kong Observatory"

# Convenience aliases callers might use → canonical HKO place name.
_STATION_ALIASES = {
    "HKO": _DEFAULT_PLACE,
    "HONG KONG OBSERVATORY": _DEFAULT_PLACE,
    "HONGKONGOBSERVATORY": _DEFAULT_PLACE,
}

# Sentinels HKO uses for "no reading" — skip these defensively.
_MISSING = {None, "", "-", "-99", "-99.0", "M", "N/A"}


class HkoObsFeed(BaseFeed):
    """Polled current-temperature feed backed by HKO ``rhrread``.

    One ``ObsTick`` per ``fetch()`` for the configured station, in native °C.
    No credentials are required, so ``is_available()`` is always ``True``.
    """

    name = "hko"

    def __init__(
        self,
        station: str = _DEFAULT_PLACE,
        bus: Optional[MarketDataBus] = None,
        config: Any = None,
    ) -> None:
        super().__init__(bus=bus)
        self.config = config
        # Resolve aliases (case-insensitive) to the canonical HKO place name.
        self.station = _STATION_ALIASES.get((station or "").strip().upper(), station)

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        # Open data, no key — always available.
        return True

    # ------------------------------------------------------------------

    async def fetch(self) -> List[Any]:
        """Fetch the current temperature for ``self.station``.

        Returns a one-element ``[ObsTick]`` on success, or ``[]`` when the
        station is not present in the payload or the value is missing. Raises
        on network/HTTP/JSON errors so ``stream()`` can back off.
        """
        params = {"dataType": "rhrread", "lang": "en"}
        headers = {"Accept": "application/json"}

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_ENDPOINT, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        temperature = (payload or {}).get("temperature") or {}
        data = temperature.get("data") or []
        if not isinstance(data, list):
            return []

        obs_time = self._parse_record_time(temperature.get("recordTime"))

        target = (self.station or "").strip().lower()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            place = str(entry.get("place", "")).strip()
            if place.lower() != target:
                continue

            raw_value = entry.get("value")
            if raw_value in _MISSING or (
                isinstance(raw_value, str) and raw_value.strip() in _MISSING
            ):
                return []
            try:
                temp = float(raw_value)
            except (TypeError, ValueError):
                return []

            # HKO reports °C; respect whatever unit the entry declares but it is
            # always "C" in practice.
            unit = str(entry.get("unit", "C")).strip().upper() or "C"
            units = "C" if unit not in {"C", "F"} else unit

            return [
                ObsTick(
                    station=place,
                    temp=temp,
                    units=units,
                    obs_time=obs_time,
                    source=self.name,
                    raw=f"{place}={raw_value}{unit} @ {temperature.get('recordTime')}",
                )
            ]

        log.debug("[feed %s] station %r not found in rhrread payload", self.name, self.station)
        return []

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_record_time(record_time: Optional[str]) -> Optional[float]:
        """Convert an ISO-8601 ``recordTime`` (e.g. ``2026-05-31T07:00:00+08:00``)
        to epoch seconds. Returns ``None`` if absent or unparseable."""
        if not record_time or not isinstance(record_time, str):
            return None
        text = record_time.strip()
        try:
            dt = _dt.datetime.fromisoformat(text)
        except ValueError:
            # Fallback for a trailing 'Z' (UTC) which older Python rejects.
            try:
                dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if dt.tzinfo is None:
            # HKO times are Hong Kong Standard Time (UTC+8).
            dt = dt.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        return dt.timestamp()
