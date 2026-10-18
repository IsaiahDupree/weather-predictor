"""
SynopticObsFeed — latest surface air temperature from Synoptic Data / MesoWest.

Source : Synoptic Data Weather API, "Latest" service.
Docs   : https://docs.synopticdata.com/services/weather-data-api
         https://docs.synopticdata.com/services/latest
Endpoint: GET https://api.synopticdata.com/v2/stations/latest

This is a *raw observation* feed: it polls a single station's most recent
``air_temp`` reading and publishes one :class:`ObsTick` per poll. Synoptic
aggregates the underlying ASOS/AWOS networks, so for a US airport ID (e.g.
``KLGA``) the latest value tracks the same METAR the market resolves against,
typically refreshed every 5 minutes (sub-hourly specials more often).

Request shape (all values verified against the official docs 2026-05-30):
  * ``stid``        — the station identifier (one station per feed instance).
  * ``token``       — public-token auth (``config.synoptic_token``).
  * ``vars=air_temp`` — only pull the temperature sensor.
  * ``units=temp|F`` — force Fahrenheit so ``ObsTick.units == 'F'`` always.
                       (Pipe-group syntax per the units doc, e.g. ``temp|F``.)
  * ``within=120``  — ignore anything older than 120 minutes (stale-guard).
  * ``obtimezone=utc`` — timestamps come back ISO-8601 in UTC with a ``Z``.

Response shape:
  ``{"STATION": [ {"STID": ..., "OBSERVATIONS": {
        "air_temp_value_1": {"date_time": "2023-08-01T23:15:00Z", "value": 22.5}
     }} ], "SUMMARY": {"RESPONSE_CODE": 1, ...}}``
A ``RESPONSE_CODE`` other than ``1`` (or a ``null`` / missing value) means there
is nothing usable, in which case ``fetch()`` returns ``[]``.

Cadence: poll every ~60s; Synoptic latest updates roughly every 5 minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_LATEST_URL = "https://api.synopticdata.com/v2/stations/latest"
_HTTP_TIMEOUT = 10.0
# Synoptic encodes missing readings as null or, occasionally, sentinel strings.
_MISSING = {None, "", "-99", "-99.0", "-9999", "-9999.0"}


class SynopticObsFeed(BaseFeed):
    """Poll Synoptic/MesoWest for one station's latest air temperature (°F)."""

    name = "synoptic"

    def __init__(
        self,
        station: str,
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus=bus)
        self.station = station.strip().upper()
        self.config = config or get_config()

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True only when a Synoptic public token is configured."""
        return bool(self.config.synoptic_token)

    # ------------------------------------------------------------------

    async def fetch(self) -> List[ObsTick]:
        """Fetch the latest air_temp for ``self.station``.

        Returns a one-element list with an :class:`ObsTick` (units ``'F'``) on
        success, or ``[]`` when the station has no recent usable value. Raises
        on a hard network/HTTP error so the base ``stream()`` can back off.
        """
        if not self.is_available():
            return []

        params = {
            "stid": self.station,
            "token": self.config.synoptic_token,
            "vars": "air_temp",
            "units": "temp|F",
            "within": "120",
            "obtimezone": "utc",
        }

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_LATEST_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()

        # SUMMARY.RESPONSE_CODE == 1 means OK; anything else => nothing usable.
        summary = payload.get("SUMMARY") or {}
        code = summary.get("RESPONSE_CODE")
        if code is not None and code != 1:
            log.debug(
                "[feed %s] %s non-OK response code=%s msg=%s",
                self.name,
                self.station,
                code,
                summary.get("RESPONSE_MESSAGE"),
            )
            return []

        stations = payload.get("STATION") or []
        if not stations:
            return []

        station_obj = stations[0]
        observations = station_obj.get("OBSERVATIONS") or {}
        air_temp = observations.get("air_temp_value_1")
        if not isinstance(air_temp, dict):
            return []

        raw_value = air_temp.get("value")
        if raw_value in _MISSING:
            return []
        try:
            temp = float(raw_value)
        except (TypeError, ValueError):
            return []

        obs_time = self._parse_iso(air_temp.get("date_time"))

        tick = ObsTick(
            station=self.station,
            temp=temp,
            units="F",
            obs_time=obs_time,
            source=self.name,
            qc=("flagged" if station_obj.get("QC_FLAGGED") else None),
            raw=f"air_temp={temp}F @ {air_temp.get('date_time')}",
        )
        return [tick]

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[float]:
        """Parse a Synoptic ISO-8601 UTC timestamp to epoch seconds.

        Synoptic returns e.g. ``"2023-08-01T23:15:00Z"`` when ``obtimezone=utc``.
        Returns ``None`` if the timestamp is missing or unparseable.
        """
        if not value or not isinstance(value, str):
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
