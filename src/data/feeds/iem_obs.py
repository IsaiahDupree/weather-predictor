"""
IemObsFeed — near-real-time US METAR/ASOS surface temperature from the Iowa
Environmental Mesonet (IEM).

Source / cadence
----------------
IEM ingests the NOAAPort SHEF/METAR feed and re-serves it through a free, no-key
"ASOS-AWOS-METAR Data Download" CGI service. Stations report at least hourly
(the routine METAR near :51 past the hour) and many ASOS sites add 5-minute
specials, so polling every 1-5 minutes is appropriate. No authentication is
required, but IEM asks every client to send a descriptive ``User-Agent`` and to
avoid hammering the service (it rate-limits with HTTP 429).

Endpoint (verified 2026-05-30 against
https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py and the IEM API docs
at https://mesonet.agron.iastate.edu/api/1/docs)::

    GET https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
        ?station=KLGA          # full 4-char ICAO (NOT the K-stripped form)
        &data=tmpf&data=metar  # repeatable; air temp (degF) + raw METAR
        &tz=UTC
        &format=onlycomma      # plain CSV, comma-delimited, no quoting
        &latlon=no
        &missing=null          # render missing values as the literal "null"
        &trace=null
        &year1=&month1=&day1=&hour1=&minute1=   # start (UTC)
        &year2=&month2=&day2=&hour2=&minute2=   # end   (UTC)

The CSV header is ``station,valid,tmpf,metar``. ``valid`` is ``YYYY-MM-DD HH:MM``
in the requested tz (UTC here). Note the ``station`` column comes back with the
leading ``K`` stripped (``LGA``) even though the request used ``KLGA`` — we keep
the caller-supplied station id on the emitted tick. ``tmpf`` is often ``null``
on the 5-minute AUTO rows; the hourly routine METAR carries the temperature, so
we scan rows newest-first for the first usable temperature, falling back to
decoding the temperature group out of the raw METAR when ``tmpf`` is missing.

Returns ObsTick(temp in degF, units='F', source='iem', raw=<metar>).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import List, Optional

import httpx

from src.config import Config, get_config
from src.data.feeds.base_feed import BaseFeed
from src.data.market_data_bus import MarketDataBus, ObsTick

log = logging.getLogger(__name__)

_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# How far back to ask for, so a single routine hourly ob is always in-window
# even if the latest 5-minute rows are all temperature-null.
_LOOKBACK_HOURS = 3.0

# Values IEM may emit for an absent measurement.
_MISSING = {"", "null", "none", "m", "t", "-99", "-99.0", "-99.00", "nan"}

# METAR temperature group: "MM/DD" with optional leading "M" (minus) on each.
# Captures the air-temperature integer in degC, e.g. "24/07" -> 24, "M03/M05" -> -3.
_METAR_TEMP_RE = re.compile(r"(?:^|\s)(M?\d{2})/(M?\d{2})(?:\s|$)")
# Higher-precision RMK Tgroup, e.g. "T02400070" -> 24.0 degC.
_METAR_TGROUP_RE = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")


class IemObsFeed(BaseFeed):
    """Poll IEM's free ASOS/METAR CGI for one US station's latest air temp (degF)."""

    name = "iem"

    def __init__(
        self,
        station: str,
        bus: Optional[MarketDataBus] = None,
        config: Optional[Config] = None,
    ) -> None:
        super().__init__(bus=bus)
        self.station = (station or "").strip().upper()
        self._config = config or get_config()
        self.name = f"iem:{self.station}" if self.station else "iem"

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """No credentials needed — only a station id."""
        return bool(self.station)

    # ------------------------------------------------------------------

    async def fetch(self) -> List[ObsTick]:
        """Return [ObsTick] for the most recent usable temperature, else []."""
        if not self.station:
            return []

        now = _dt.datetime.now(_dt.timezone.utc)
        start = now - _dt.timedelta(hours=_LOOKBACK_HOURS)

        params = [
            ("station", self.station),
            ("data", "tmpf"),
            ("data", "metar"),
            ("tz", "UTC"),
            ("format", "onlycomma"),
            ("latlon", "no"),
            ("missing", "null"),
            ("trace", "null"),
            ("year1", str(start.year)),
            ("month1", str(start.month)),
            ("day1", str(start.day)),
            ("hour1", str(start.hour)),
            ("minute1", str(start.minute)),
            ("year2", str(now.year)),
            ("month2", str(now.month)),
            ("day2", str(now.day)),
            ("hour2", str(now.hour)),
            ("minute2", str(now.minute)),
        ]
        headers = {
            "User-Agent": self._config.iem_user_agent,
            "Accept": "text/csv, text/plain",
        }

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(_ASOS_URL, params=params)
            resp.raise_for_status()
            body = resp.text

        tick = self._parse_latest(body)
        return [tick] if tick is not None else []

    # ------------------------------------------------------------------

    def _parse_latest(self, body: str) -> Optional[ObsTick]:
        """Find the newest row carrying a usable temperature (tmpf or METAR)."""
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if not lines:
            return None

        # Map header columns -> index (header is "station,valid,tmpf,metar").
        header = [h.strip().lower() for h in lines[0].split(",")]
        try:
            i_valid = header.index("valid")
        except ValueError:
            i_valid = 1
        i_tmpf = header.index("tmpf") if "tmpf" in header else 2
        i_metar = header.index("metar") if "metar" in header else None

        # Walk rows newest-first; rows are ascending by time, so reverse.
        for raw_row in reversed(lines[1:]):
            # METAR strings contain spaces but no commas, so a plain split is
            # safe for this fixed-shape onlycomma output.
            cols = raw_row.split(",")
            if len(cols) <= i_valid:
                continue

            metar = ""
            if i_metar is not None and len(cols) > i_metar:
                metar = ",".join(cols[i_metar:]).strip()

            temp_f = self._cell_float(cols[i_tmpf]) if len(cols) > i_tmpf else None
            if temp_f is None and metar:
                temp_f = self._temp_from_metar(metar)
            if temp_f is None:
                continue

            obs_time = self._parse_valid(cols[i_valid].strip())
            return ObsTick(
                station=self.station,
                temp=round(temp_f, 1),
                units="F",
                obs_time=obs_time,
                source="iem",
                raw=metar or None,
            )

        return None

    # ------------------------------------------------------------------

    @staticmethod
    def _cell_float(cell: str) -> Optional[float]:
        v = (cell or "").strip()
        if v.lower() in _MISSING:
            return None
        try:
            f = float(v)
        except ValueError:
            return None
        # IEM sentinel for missing when missing= is not honored for a column.
        if f <= -98.0:
            return None
        return f

    @staticmethod
    def _parse_valid(valid: str) -> Optional[float]:
        """'YYYY-MM-DD HH:MM' (UTC) -> epoch seconds."""
        valid = valid.strip()
        if not valid or valid.lower() in _MISSING:
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                dt = _dt.datetime.strptime(valid, fmt).replace(
                    tzinfo=_dt.timezone.utc
                )
                return dt.timestamp()
            except ValueError:
                continue
        return None

    @classmethod
    def _temp_from_metar(cls, metar: str) -> Optional[float]:
        """Decode air temperature (degF) from a raw METAR string.

        Prefer the high-precision RMK ``T`` group (tenths of degC) and fall back
        to the body ``TT/DD`` group (whole degC). Returns degF or None.
        """
        if not metar:
            return None

        m = _METAR_TGROUP_RE.search(metar)
        if m:
            sign = -1.0 if m.group(1) == "1" else 1.0
            temp_c = sign * int(m.group(2)) / 10.0
            return temp_c * 9.0 / 5.0 + 32.0

        m = _METAR_TEMP_RE.search(metar)
        if m:
            temp_c = cls._signed_int(m.group(1))
            if temp_c is not None:
                return temp_c * 9.0 / 5.0 + 32.0
        return None

    @staticmethod
    def _signed_int(token: str) -> Optional[float]:
        """'M03' -> -3.0, '24' -> 24.0."""
        token = token.strip()
        if not token:
            return None
        neg = token.startswith("M")
        digits = token[1:] if neg else token
        if not digits.isdigit():
            return None
        val = float(int(digits))
        return -val if neg else val
