"""
KMA (Korea Meteorological Administration) ASOS surface observation feed.

Source
------
KMA API Hub — "지상관측(ASOS) 시간자료" (surface hourly observation).

    GET https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php
        ?tm={YYYYMMDDHHMM}   # observation time, top of the current UTC hour... see note
        &stn={station}       # ASOS station number (Seoul = 108)
        &help=1              # include the column-header legend in the response
        &authKey={kma_auth_key}

Reference: https://apihub.kma.go.kr  (typ01 / kma_sfctm3.php).

Response format
---------------
Plain text, NOT JSON. The body is a fixed-width / whitespace-delimited table
where every comment, legend and structural line is prefixed with ``#``. With
``help=1`` the legend block lists the column names in order, e.g.::

    #START7777
    # YYMMDDHHMI STN  WD   WS   GST ... PA      PS     PT  PR   TA   TD   HM ...
    # ...
    202405300100 108  250   3.4 ...   1009.1 1018.3   ...  17.2  9.4  60 ...
    #7777END

``TA`` is the **air temperature in °C**. We locate the ``TA`` column by parsing
the legend line (the ``#`` comment line whose tokens include ``TA``) and using
its position; if the legend cannot be parsed we fall back to the documented
canonical index (TA is the 12th data field, 0-based index 11, in kma_sfctm3).

Missing values in this feed are encoded as negative sentinels (``-9``, ``-99``,
``-999``, ``-9.0`` ...). Those are skipped rather than published as a real temp.

Cadence
-------
ASOS surface obs publish hourly (and the API exposes minute granularity for the
top of the hour). Polling once every ~5-15 min is sufficient; a repeated value
for the same ``obs_time`` is harmless because the bus keys on the latest tick.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_KMA_BASE = "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php"
_TIMEOUT = 10.0

# Canonical column order for kma_sfctm3 (per the help legend). TA = air
# temperature (°C) sits at 0-based index 11 of the whitespace-split data row:
#   0:TM 1:STN 2:WD 3:WS 4:GST_WD 5:GST_WS 6:GST_TM 7:PA 8:PS 9:PT 10:PR 11:TA
_FALLBACK_TA_INDEX = 11

# KMA encodes "no value" with negative sentinels; treat anything <= this as
# missing. Real surface air temperatures in Korea never approach -50 °C.
_MISSING_THRESHOLD = -50.0


def _is_missing(value: float) -> bool:
    """KMA missing-data sentinels are negative magic numbers (-9, -99, -999...)."""
    return value <= _MISSING_THRESHOLD


def _find_ta_index(lines: List[str]) -> int:
    """Locate the 0-based column index of ``TA`` from the ``#`` legend line.

    The help legend is emitted as ``#``-prefixed lines; the relevant one lists
    the column names. We pick the comment line that contains the bare token
    ``TA`` (and also ``STN``, to avoid matching prose) and return TA's position.
    Falls back to the documented canonical index when no legend is present.
    """
    for raw in lines:
        if not raw.startswith("#"):
            continue
        tokens = raw.lstrip("#").split()
        # A header/legend line, not a prose comment: must contain the key fields.
        if "TA" in tokens and "STN" in tokens:
            return tokens.index("TA")
    return _FALLBACK_TA_INDEX


class KmaObsFeed(BaseFeed):
    """Poll KMA API Hub ASOS hourly surface obs for a single station.

    ``temp`` is reported in the station's native units, which for KMA is **°C**;
    each tick is published as ``ObsTick(..., units="C", source="kma")``.
    """

    name = "kma"

    def __init__(
        self,
        station: str = "108",
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus=bus)
        self.station = str(station)
        self._config = config or get_config()
        self.name = f"kma:{self.station}"

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._config.kma_auth_key)

    # ------------------------------------------------------------------

    @staticmethod
    def _current_hour_tm(now: Optional[float] = None) -> str:
        """``YYYYMMDDHHMM`` for the top of the current UTC hour.

        kma_sfctm3 keys observations on the on-the-hour minute, so we zero the
        minutes. KMA serves the most recent available record at/under ``tm``.
        """
        dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
        dt = dt.replace(minute=0, second=0, microsecond=0)
        return dt.strftime("%Y%m%d%H%M")

    @staticmethod
    def _parse_obs_time(tm_field: str) -> Optional[float]:
        """Parse the leading ``YYYYMMDDHHMM`` time field into epoch seconds (UTC)."""
        tm = tm_field.strip()
        if len(tm) < 12 or not tm[:12].isdigit():
            return None
        try:
            dt = datetime.strptime(tm[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return dt.timestamp()

    # ------------------------------------------------------------------

    async def fetch(self) -> List[ObsTick]:
        """Fetch the latest ASOS temperature for ``self.station``.

        Returns a single-element ``[ObsTick]`` on success, or ``[]`` when the
        value is missing/unparseable. Raises on network/auth/HTTP errors so the
        base ``stream()`` can back off.
        """
        if not self.is_available():
            return []

        params = {
            "tm": self._current_hour_tm(),
            "stn": self.station,
            "help": "1",
            "authKey": self._config.kma_auth_key,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_KMA_BASE, params=params)
            resp.raise_for_status()
            text = resp.text

        return self._parse(text)

    # ------------------------------------------------------------------

    def _parse(self, text: str) -> List[ObsTick]:
        """Parse the fixed-width KMA text body into at most one ObsTick."""
        if not text:
            return []

        lines = text.splitlines()
        ta_index = _find_ta_index(lines)

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue  # comment / legend / structural marker

            fields = line.split()
            if len(fields) <= ta_index:
                continue

            # Confirm this row is for our station (field index 1 = STN).
            if len(fields) > 1 and fields[1].strip() != self.station:
                continue

            ta_raw = fields[ta_index]
            try:
                temp_c = float(ta_raw)
            except ValueError:
                continue
            if _is_missing(temp_c):
                continue

            obs_time = self._parse_obs_time(fields[0]) if fields else None

            return [
                ObsTick(
                    station=self.station,
                    temp=temp_c,
                    units="C",
                    obs_time=obs_time,
                    source="kma",
                    raw=line[:256],
                )
            ]

        return []
