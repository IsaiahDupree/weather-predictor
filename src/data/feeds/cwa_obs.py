"""
CwaObsFeed — Taiwan Central Weather Administration (CWA) automatic-station
surface observations.

Source : CWA Open Data Platform, dataset ``O-A0003-001``
         ("自動氣象站-氣象觀測資料" / Automatic Weather Station observations).
Docs   : https://opendata.cwa.gov.tw/dist/opendata-swagger.html
         https://opendata.cwa.gov.tw/dataset/observation/O-A0003-001
Cadence: stations report roughly every ~10 minutes; poll no faster than that.

Endpoint (verified against the live API + the dataset schema, May 2026):

    GET https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001
        ?Authorization=<CWA_AUTH_KEY>      # API key, passed as a *query* param
        &format=JSON
        &StationId=466920                  # 466920 = 臺北 (Taipei) observatory

The key is accepted either as the ``Authorization`` query parameter *or* an
``Authorization`` HTTP header; we use the query parameter per the swagger spec.
A request without (or with a bad) key returns HTTP 401.

Response shape (current "Station" schema — newer than the deprecated
``records.location[].weatherElement[]`` form)::

    {
      "success": "true",
      "records": {
        "Station": [
          {
            "StationId":   "466920",
            "StationName": "臺北",
            "ObsTime":     {"DateTime": "2026-05-30T12:00:00+08:00"},
            "WeatherElement": {
              "AirTemperature": 28.4,            # degrees Celsius
              ...
            },
            ...
          }
        ]
      }
    }

Air temperature is in °C. Missing values arrive as the sentinel ``-99`` /
``-99.0`` (and occasionally string forms such as ``"-99"``, ``"***"`` or
``"X"``); those rows are skipped. We emit ``ObsTick`` objects with
``units="C"`` and ``obs_time`` set from ``ObsTime.DateTime`` (parsed as
timezone-aware ISO-8601 and converted to epoch seconds).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

# CWA Open Data REST datastore endpoint for dataset O-A0003-001.
_CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001"

# Missing-data sentinels. CWA uses numeric -99 / -99.0 for "no reading"; string
# variants like "***", "X", "N/A" also appear across CWA datasets.
_MISSING_STR = {"", "-99", "-99.0", "***", "x", "X", "n/a", "na", "null", "none"}


class CwaObsFeed(BaseFeed):
    """Poll one CWA automatic station's current air temperature.

    Parameters
    ----------
    station:
        CWA StationId (e.g. ``"466920"`` for Taipei). Used both as the API
        ``StationId`` filter and as the published ``ObsTick.station``.
    bus:
        Optional :class:`MarketDataBus` to publish into (handled by the base).
    config:
        Optional :class:`Config`; defaults to the process singleton. The auth
        key is read from ``config.cwa_auth_key``.
    """

    name = "cwa"

    def __init__(
        self,
        station: str,
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus=bus)
        self.station = str(station).strip()
        self._config = config or get_config()
        self.name = f"cwa:{self.station}" if self.station else "cwa"

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True when a CWA auth key is configured."""
        return bool(self._config.cwa_auth_key)

    # ------------------------------------------------------------------

    async def fetch(self) -> List[ObsTick]:
        """Fetch the station's latest temperature; return ``[]`` if unusable.

        Raises on network/HTTP/auth errors so ``stream()`` can back off; returns
        ``[]`` when the station is present but its temperature is missing.
        """
        if not self.is_available():
            return []

        params = {
            "Authorization": self._config.cwa_auth_key,
            "format": "JSON",
        }
        if self.station:
            params["StationId"] = self.station

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_CWA_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        return self._parse(data)

    # ------------------------------------------------------------------

    def _parse(self, data: Dict[str, Any]) -> List[ObsTick]:
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, dict):
            return []

        # Current schema: records.Station[]. Tolerate the legacy
        # records.location[]/weatherElement[] form as a fallback.
        stations = records.get("Station")
        if isinstance(stations, dict):
            stations = [stations]
        if not isinstance(stations, list) or not stations:
            return self._parse_legacy(records)

        ticks: List[ObsTick] = []
        for st in stations:
            if not isinstance(st, dict):
                continue
            tick = self._station_to_tick(st)
            if tick is not None:
                ticks.append(tick)
        return ticks

    def _station_to_tick(self, st: Dict[str, Any]) -> Optional[ObsTick]:
        wx = st.get("WeatherElement")
        if not isinstance(wx, dict):
            return None

        temp = self._safe_temp(wx.get("AirTemperature"))
        if temp is None:
            return None

        station_id = str(st.get("StationId") or self.station or "").strip()
        if not station_id:
            station_id = self.station

        obs_dt = st.get("ObsTime")
        obs_time = None
        if isinstance(obs_dt, dict):
            obs_time = self._parse_obs_time(obs_dt.get("DateTime"))

        return ObsTick(
            station=station_id,
            temp=temp,
            units="C",
            obs_time=obs_time,
            source="cwa",
        )

    def _parse_legacy(self, records: Dict[str, Any]) -> List[ObsTick]:
        """Fallback for the deprecated records.location[]/weatherElement[] form."""
        locations = records.get("location")
        if not isinstance(locations, list):
            return []

        ticks: List[ObsTick] = []
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            elements = loc.get("weatherElement")
            if not isinstance(elements, list):
                continue
            temp = None
            for el in elements:
                if not isinstance(el, dict):
                    continue
                if el.get("elementName") in ("TEMP", "AirTemperature", "TX"):
                    temp = self._safe_temp(el.get("elementValue"))
                    break
            if temp is None:
                continue
            station_id = str(loc.get("stationId") or self.station or "").strip()
            obs_time = self._parse_obs_time(loc.get("time", {}).get("obsTime")
                                            if isinstance(loc.get("time"), dict) else None)
            ticks.append(
                ObsTick(
                    station=station_id or self.station,
                    temp=temp,
                    units="C",
                    obs_time=obs_time,
                    source="cwa",
                )
            )
        return ticks

    # ------------------------------------------------------------------

    @staticmethod
    def _safe_temp(value: Any) -> Optional[float]:
        """Parse an air-temperature field, returning None for missing sentinels."""
        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in _MISSING_STR:
            return None
        try:
            temp = float(text)
        except (TypeError, ValueError):
            return None
        # Numeric missing sentinel (-99 / -99.0); real surface temps never hit it.
        if temp <= -90.0:
            return None
        return temp

    @staticmethod
    def _parse_obs_time(value: Any) -> Optional[float]:
        """Convert CWA ISO-8601 ('...+08:00') to epoch seconds, or None."""
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Normalize a trailing 'Z' just in case; CWA uses an explicit +08:00.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        try:
            return dt.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
