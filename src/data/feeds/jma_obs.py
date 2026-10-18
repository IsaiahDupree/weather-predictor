"""
JmaObsFeed — surface temperature observations from the Japan Meteorological
Agency (JMA) AMeDAS network.

Source / cadence
----------------
JMA publishes the AMeDAS "map" dataset that powers the public viewer at
https://www.jma.go.jp/bosai/amedas/ . It is a free, keyless static-JSON feed
refreshed roughly every 10 minutes (each AMeDAS station reports 10-minute
observations). Two requests per poll:

  1. GET https://www.jma.go.jp/bosai/amedas/data/latest_time.txt
     -> a single ISO-8601 JST timestamp, e.g. ``2026-05-31T08:10:00+09:00``.
     This is the valid time of the most recent published snapshot.

  2. GET https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json
     where ``{ts}`` is that timestamp's JST wall-clock formatted as
     ``yyyymmddHHMMSS`` (the file path uses local Japan time, not UTC).
     The body is a JSON object keyed by AMeDAS station-number strings.
     Each station entry is a dict of ``field -> [value, qc_flag]`` pairs, e.g.::

       "44132": {"temp": [21.3, 0], "humidity": [57, 0], "wind": [3.1, 0], ...}

     ``temp[0]`` is the air temperature in degrees Celsius; ``temp[1]`` is the
     JMA QC flag (0 == normal). Missing sensors omit the field entirely or use
     a null value, which we skip.

Default station 44132 is Tokyo / Otemachi (the official Tokyo observation
point used by JMA, lat 35.69, lon 139.75). Temperatures are emitted as
``ObsTick`` with ``units='C'`` and ``source='jma'``; no conversion is done here
(downstream compares against the market's native units and converts as needed).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_LATEST_TIME_URL = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
_MAP_URL_TMPL = "https://www.jma.go.jp/bosai/amedas/data/map/{ts}.json"

# Japan Standard Time — AMeDAS file paths are keyed on JST wall-clock.
_JST = _dt.timezone(_dt.timedelta(hours=9))

# Values JMA uses to mark "no data" in addition to None.
_MISSING_SENTINELS = {None, -99, -99.0, -999, -999.0, -9999, -9999.0}


class JmaObsFeed(BaseFeed):
    """Poll JMA AMeDAS for a single station's latest 10-minute temperature.

    Keyless public feed, so :meth:`is_available` is always ``True``.

    Example
    -------
    >>> feed = JmaObsFeed("44132")           # Tokyo / Otemachi
    >>> ticks = await feed.fetch()
    >>> ticks and ticks[0].units == "C"
    """

    name = "jma"

    def __init__(
        self,
        station: str = "44132",
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus=bus)
        # AMeDAS station numbers are strings (preserve any leading zeros).
        self.station = str(station).strip()
        self.config = config or get_config()
        self._last_ts: Optional[str] = None

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """JMA AMeDAS is a free, keyless public feed."""
        return True

    # ------------------------------------------------------------------

    async def fetch(self) -> List[Any]:
        """Fetch this station's most recent temperature observation.

        Returns a one-element ``[ObsTick]`` list, or ``[]`` when the station is
        absent from the snapshot, its temperature is missing/QC-rejected, or the
        published snapshot has not advanced since the previous successful poll.
        Raises on hard network/HTTP errors so ``stream()`` can back off.
        """
        headers = {
            "Accept": "text/plain, application/json",
            "User-Agent": "weatherpredictor/1.0 (+jma-amedas)",
        }
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            ts, obs_epoch = await self._latest_timestamp(client)

            # Nothing new since last successful poll — avoid duplicate ticks.
            if ts == self._last_ts:
                return []

            resp = await client.get(_MAP_URL_TMPL.format(ts=ts))
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, dict):
            log.warning("[feed %s] unexpected map payload type: %r", self.name, type(data))
            return []

        entry = data.get(self.station)
        if not isinstance(entry, dict):
            # Station not present in this snapshot (sensor offline / wrong id).
            self._last_ts = ts
            return []

        temp_c = self._read_temp(entry.get("temp"))
        if temp_c is None:
            self._last_ts = ts
            return []

        qc = self._read_qc(entry.get("temp"))
        self._last_ts = ts

        tick = ObsTick(
            station=self.station,
            temp=temp_c,
            units="C",
            obs_time=obs_epoch,
            source=self.name,
            qc=qc,
        )
        return [tick]

    # ------------------------------------------------------------------

    async def _latest_timestamp(self, client: httpx.AsyncClient) -> tuple[str, float]:
        """Return ``(yyyymmddHHMMSS_jst, valid_epoch_seconds_utc)``.

        ``latest_time.txt`` carries a JST ISO-8601 string; the map file path
        uses the JST wall-clock, while ``obs_time`` on the tick is the true UTC
        epoch of that instant.
        """
        resp = await client.get(_LATEST_TIME_URL)
        resp.raise_for_status()
        raw = (resp.text or "").strip()
        if not raw:
            raise ValueError("JMA latest_time.txt returned empty body")

        # e.g. "2026-05-31T08:10:00+09:00"
        moment = _dt.datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_JST)

        ts = moment.astimezone(_JST).strftime("%Y%m%d%H%M%S")
        epoch = moment.timestamp()
        return ts, epoch

    @staticmethod
    def _read_temp(field: Any) -> Optional[float]:
        """Extract Celsius temperature from a ``[value, qc]`` pair.

        Returns ``None`` for missing/sentinel values so the caller can skip.
        """
        if not isinstance(field, (list, tuple)) or not field:
            return None
        value = field[0]
        if value in _MISSING_SENTINELS:
            return None
        try:
            temp = float(value)
        except (TypeError, ValueError):
            return None
        # Defensive: JMA never reports surface temps outside this range.
        if temp < -90.0 or temp > 60.0:
            return None
        return temp

    @staticmethod
    def _read_qc(field: Any) -> Optional[str]:
        """Extract the JMA QC flag (second element) as a string, if present."""
        if not isinstance(field, (list, tuple)) or len(field) < 2:
            return None
        qc = field[1]
        if qc is None:
            return None
        return str(qc)
