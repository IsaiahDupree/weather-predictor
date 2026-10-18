"""
NOAA NCEI final daily-Tmax client (ground truth, lagged — backtest only).

The authoritative settlement temperature for US weather markets is the official
daily maximum recorded by the NWS/ASOS station and quality-controlled into the
**Global Historical Climatology Network - Daily (GHCND)** dataset. NCEI exposes
GHCND through the public **Data Access Service v1**, which needs no token (the
older Climate Data Online "CDO" web service is the one that requires a token —
this is a different, open service).

Because GHCND values are the *final, QC'd* daily summaries, they lag real time
by roughly a day or more and are therefore unsuitable for live trading. This
client exists purely to provide **ground-truth labels for backtesting and
settlement reconciliation**.

Source:   https://www.ncei.noaa.gov/access/services/data/v1
          https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation
Endpoint: GET https://www.ncei.noaa.gov/access/services/data/v1
          ?dataset=daily-summaries
          &stations={ghcnd_station}
          &dataTypes=TMAX
          &startDate={YYYY-MM-DD}
          &endDate={YYYY-MM-DD}
          &units=standard            # standard => TMAX in degrees Fahrenheit
          &format=json
Cadence:  GHCND finalizes ~1+ day after the observation date (often longer for
          full QC). Poll at most a few times per day; the value for a given past
          date is effectively static once published. No auth required.

Verified live (2026): the endpoint returns a JSON array of per-day records,
each shaped like::

    {"DATE": "2024-06-15", "STATION": "USW00094728", "TMAX": "80"}

With ``units=standard`` the TMAX field is the daily maximum in whole degrees
Fahrenheit, returned as a *string*. Missing days are simply absent from the
array (the service omits them rather than emitting a sentinel), and any
"-9999"/null GHCND sentinel is treated as missing.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Dict, Optional, Tuple

import httpx

from src.config import Config, get_config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult

_BASE_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
_TIMEOUT = 10.0

# ICAO -> GHCND station id, for US ASOS stations used by the weather markets.
# Every id below was verified against the NCEI station registry (the Data
# Access Service returns the station NAME/LAT/LON alongside each record):
#   KNYC USW00094728  NY CITY CENTRAL PARK, NY US
#   KLGA USW00014732  LAGUARDIA AIRPORT, NY US
#   KMDW USW00014819  CHICAGO MIDWAY AIRPORT, IL US
#   KLAX USW00023174  LOS ANGELES INTERNATIONAL AIRPORT, CA US
#   KMIA USW00012839  MIAMI INTERNATIONAL AIRPORT, FL US
#   KDEN USW00003017  DENVER INTERNATIONAL AIRPORT, CO US
#   KAUS USW00013904  AUSTIN BERGSTROM INTERNATIONAL AIRPORT, TX US
#   KIAH USW00012960  HOUSTON INTERCONTINENTAL AIRPORT, TX US
#   KPHL USW00013739  PHILADELPHIA INTERNATIONAL AIRPORT, PA US
STATION_GHCND: Dict[str, str] = {
    "KNYC": "USW00094728",
    "KLGA": "USW00014732",
    "KMDW": "USW00014819",
    "KLAX": "USW00023174",
    "KMIA": "USW00012839",
    "KDEN": "USW00003017",
    "KAUS": "USW00013904",
    "KIAH": "USW00012960",
    "KPHL": "USW00013739",
}

# Approximate (lat, lon) for each ICAO station, taken from the NCEI registry
# response (LATITUDE/LONGITUDE fields). Used only for nearest-ICAO matching in
# ``daily_tmax`` so a (lat, lon) forecast request can be resolved to a GHCND id.
_STATION_LATLON: Dict[str, Tuple[float, float]] = {
    "KNYC": (40.77898, -73.96925),
    "KLGA": (40.77945, -73.88027),
    "KMDW": (41.78412, -87.75514),
    "KLAX": (33.93816, -118.38660),
    "KMIA": (25.78805, -80.31694),
    "KDEN": (39.84657, -104.65623),
    "KAUS": (30.18311, -97.67989),
    "KIAH": (29.98438, -95.36072),
    "KPHL": (39.87326, -75.22681),
}

# GHCND uses -9999 for missing; some downstream feeds use -99. Anything at or
# below this threshold (in either F or the raw sentinel range) is "missing".
_MISSING_SENTINEL = -99.0

# If the nearest known ICAO is farther than this from the requested point, we
# refuse to guess a station rather than label with a wildly wrong site.
_MAX_MATCH_KM = 75.0


class NceiFinalClient(ForecastClient):
    """Final (lagged) daily-Tmax ground truth from NOAA NCEI GHCND.

    This is a backtest/settlement source, not a live forecast. It implements
    the :class:`ForecastClient` interface so it can slot into the same plumbing
    as the forward clients, but its values are observed truth, not predictions.
    """

    name = "ncei_final"

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        # The Data Access Service is a public, token-free endpoint, so the
        # client is always available (network errors are handled per-call).
        return True

    # ------------------------------------------------------------------

    async def final_tmax(
        self,
        ghcnd_station: str,
        target_date: _dt.date,
    ) -> Optional[float]:
        """Return the final QC'd daily maximum (degrees Fahrenheit) for a date.

        ``ghcnd_station`` is a GHCND id (e.g. ``"USW00094728"``); an ICAO code
        present in :data:`STATION_GHCND` (e.g. ``"KNYC"``) is also accepted and
        resolved. Returns ``None`` when the value is missing/not yet published,
        the request fails, or the station is unknown.
        """
        station = self._resolve_station(ghcnd_station)
        if not station:
            return None

        iso = target_date.isoformat()
        params = {
            "dataset": "daily-summaries",
            "stations": station,
            "dataTypes": "TMAX",
            "startDate": iso,
            "endDate": iso,
            "units": "standard",   # standard => Fahrenheit
            "format": "json",
        }
        headers = {
            "Accept": "application/json",
            # NCEI does not require a UA, but identifying the client is polite.
            "User-Agent": self._config.nws_user_agent or "weatherpredictor/1.0",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(_BASE_URL, params=params, headers=headers)
                resp.raise_for_status()
                # The service returns 200 with an empty body when there is no
                # data for the window; json() would then raise.
                if not resp.content or not resp.text.strip():
                    return None
                payload = resp.json()
        except (httpx.HTTPError, ValueError):
            # Network/HTTP errors or a non-JSON body => treat as "no value".
            return None

        if not isinstance(payload, list) or not payload:
            return None

        # Pick the record matching the requested date (single-day window
        # normally returns exactly one record, but match defensively).
        record = None
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            date_str = entry.get("DATE")
            if isinstance(date_str, str) and date_str[:10] == iso:
                record = entry
                break
            if record is None:
                record = entry

        if not isinstance(record, dict):
            return None

        return self._read_tmax(record.get("TMAX"))

    # ------------------------------------------------------------------

    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: _dt.date,
        units: str = "F",
    ) -> ForecastResult:
        """:class:`ForecastClient` adapter: resolve (lat, lon) to nearest ICAO.

        GHCND is keyed by station, not arbitrary coordinates, so this method
        snaps the requested point to the nearest known ICAO in
        :data:`STATION_GHCND` (within :data:`_MAX_MATCH_KM`) and returns its
        final Tmax as a single ``point``. If no station is close enough, the
        result carries ``point=None`` (there is no meaningful forecast to give
        for an unmapped location — this is a settlement source, not a model).
        """
        out_units = "C" if str(units).upper().startswith("C") else "F"
        result = ForecastResult(
            source=self.name,
            target_date=target_date.isoformat(),
            units=out_units,
            members=[],
            point=None,
        )

        icao = self._nearest_icao(lat, lon)
        if icao is None:
            # No mapped station within range: nothing to settle against.
            return result

        ghcnd = STATION_GHCND[icao]
        tmax_f = await self.final_tmax(ghcnd, target_date)
        if tmax_f is None:
            return result

        result.point = self._to_celsius(tmax_f) if out_units == "C" else tmax_f
        result.source = f"{self.name}:{icao}"
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_station(station: str) -> str:
        """Accept either a GHCND id or a mapped ICAO code; return a GHCND id."""
        if not station:
            return ""
        key = station.strip().upper()
        if key in STATION_GHCND:
            return STATION_GHCND[key]
        return key  # assume it is already a GHCND id

    @staticmethod
    def _read_tmax(raw: object) -> Optional[float]:
        """Parse the TMAX field (a string in standard units => Fahrenheit).

        Returns ``None`` for null/empty values and for missing-data sentinels
        (GHCND -9999, or a -99 used by some feeds).
        """
        if raw is None:
            return None
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return None
        if num <= _MISSING_SENTINEL:
            return None
        return num

    @staticmethod
    def _to_celsius(temp_f: float) -> float:
        return round((temp_f - 32.0) * 5.0 / 9.0, 2)

    @classmethod
    def _nearest_icao(cls, lat: float, lon: float) -> Optional[str]:
        """Return the nearest mapped ICAO within ``_MAX_MATCH_KM``, else None."""
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            return None

        best_icao: Optional[str] = None
        best_km = float("inf")
        for icao, (s_lat, s_lon) in _STATION_LATLON.items():
            km = cls._haversine_km(lat_f, lon_f, s_lat, s_lon)
            if km < best_km:
                best_km = km
                best_icao = icao
        if best_icao is None or best_km > _MAX_MATCH_KM:
            return None
        return best_icao

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in kilometres between two lat/lon points."""
        radius_km = 6371.0088
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)
        a = (
            math.sin(d_phi / 2.0) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2.0) ** 2
        )
        return 2.0 * radius_km * math.asin(min(1.0, math.sqrt(a)))
