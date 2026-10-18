"""
NwsObsFeed — latest surface observation for a single station from the U.S.
National Weather Service public API (``api.weather.gov``).

Source / cadence
----------------
* Endpoint: ``GET https://api.weather.gov/stations/{station}/observations/latest``
* Docs:     https://www.weather.gov/documentation/services-web-api
            (full schema: https://api.weather.gov/openapi.json)
* Auth:     none, but a descriptive ``User-Agent`` header is REQUIRED — the API
            returns ``403 Forbidden`` to requests without one.
* Cadence:  ASOS/AWOS stations publish a new METAR roughly hourly (with
            occasional SPECI specials), so polling more often than ~5 min is
            wasteful. The feed simply returns whatever the "latest" endpoint
            holds; ``ObsTick.obs_time`` carries the observation's own valid
            time so the bus can detect when nothing new has arrived.

Response shape (verified live against KLGA, 2026-05-30)
-------------------------------------------------------
GeoJSON ``Feature`` whose ``properties`` block contains::

    "timestamp":   "2026-05-30T23:00:00+00:00"        # ISO-8601, UTC offset
    "rawMessage":  "KLGA 302351Z ..."                 # raw METAR (may be "")
    "temperature": {"unitCode": "wmoUnit:degC",        # ALWAYS Celsius
                    "value": 16,                       # may be null
                    "qualityControl": "V"}

The temperature ``value`` is reported in degrees Celsius (``unitCode`` =
``wmoUnit:degC``); we publish it verbatim with ``units="C"`` and let downstream
consumers convert against the market's resolution units. A ``null`` value (or a
missing temperature block) means the station has no current reading — we skip
it and return ``[]`` rather than emitting a junk tick.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_API_BASE = "https://api.weather.gov"
_TIMEOUT = 10.0


class NwsObsFeed(BaseFeed):
    """Polls ``api.weather.gov`` for one station's latest temperature obs.

    Parameters
    ----------
    station:
        NWS station identifier (e.g. ``"KLGA"``, ``"KNYC"``). Used directly in
        the request path and as ``ObsTick.station``.
    bus:
        Optional :class:`MarketDataBus` to publish ticks to (handled by the
        base ``poll_once``); may be ``None`` for one-shot ``fetch()`` use.
    config:
        Optional :class:`Config`; falls back to the process singleton. Supplies
        the required ``nws_user_agent`` header value.
    """

    name = "nws"

    def __init__(
        self,
        station: str,
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus)
        self.station = (station or "").strip().upper()
        self._config = config or get_config()
        self._user_agent = self._config.nws_user_agent

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Always available — no credentials needed, only a station id.

        The API is free and keyless; the only hard requirement is a station
        identifier and a User-Agent header (the latter always has a default in
        :class:`Config`).
        """
        return bool(self.station)

    # ------------------------------------------------------------------

    async def fetch(self) -> List[ObsTick]:
        """Fetch the latest observation, returning ``[ObsTick]`` or ``[]``.

        Raises on a hard transport/HTTP error so the base ``stream()`` can back
        off; returns ``[]`` when the station has no usable temperature reading.
        """
        if not self.station:
            return []

        url = f"{_API_BASE}/stations/{self.station}/observations/latest"
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/geo+json",
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            # 404 => station id unknown / no current obs: not a hard error.
            if resp.status_code == 404:
                log.debug("[feed %s] %s -> 404 (no latest obs)", self.name, self.station)
                return []
            resp.raise_for_status()
            payload = resp.json()

        props = (payload or {}).get("properties") or {}
        temp_block = props.get("temperature") or {}

        value = temp_block.get("value")
        if value is None:
            # No current temperature for this station — nothing to publish.
            log.debug("[feed %s] %s temperature.value is null", self.name, self.station)
            return []

        try:
            temp_c = float(value)
        except (TypeError, ValueError):
            log.debug("[feed %s] %s non-numeric temperature.value=%r",
                      self.name, self.station, value)
            return []

        # Guard against missing-data sentinels some upstreams use.
        if temp_c <= -99.0:
            return []

        qc = temp_block.get("qualityControl")
        obs_time = self._parse_iso(props.get("timestamp"))
        raw = props.get("rawMessage") or None

        tick = ObsTick(
            station=self.station,
            temp=temp_c,
            units="C",            # unitCode is wmoUnit:degC — always Celsius
            obs_time=obs_time,
            source=self.name,
            qc=qc if qc else None,
            raw=raw,
        )
        return [tick]

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[float]:
        """Parse an ISO-8601 timestamp (e.g. ``2026-05-30T23:00:00+00:00``)
        into epoch seconds (UTC). Returns ``None`` if unparseable/absent."""
        if not value:
            return None
        text = str(value).strip()
        # Python <3.11 ``fromisoformat`` rejects a trailing 'Z'; normalize it.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
