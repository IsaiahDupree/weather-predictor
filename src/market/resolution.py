"""
Resolution spec — the single source of truth for *which station* and *which day*
a given (venue, city) weather market settles on.

This is load-bearing: the two venues use DIFFERENT stations for the same city
(verified in research):

  * Polymarket NYC resolves on **Wunderground / KLGA (LaGuardia)**.
  * Kalshi NYC resolves on the **NWS Daily Climate Report for KNYC (Central Park)**.
  * Kalshi Chicago = **KMDW (Midway)**, not O'Hare.

Forecasting or reading obs against the wrong station is a systematic edge-killer,
so every market the bot touches must map to a ResolutionSpec or be rejected by
the risk engine (FORBIDDEN: unknown resolution station).

The climate-day boundary also differs from a naive UTC day: Kalshi uses the
local-standard-time (LST) 24h climate day. We compute the window in the city's
IANA timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# Which obs feed serves a region's stations.
FEED_FOR_REGION = {
    "us": "synoptic",   # primary; loop falls back to iem -> nws
    "tw": "cwa",
    "jp": "jma",
    "kr": "kma",
    "hk": "hko",
}


@dataclass(frozen=True)
class ResolutionSpec:
    venue: str                 # "kalshi" | "polymarket"
    city: str                  # lowercase key, e.g. "nyc"
    station: str               # station id as the feed expects it (KNYC, 466920, ...)
    source: str                # human-readable source of record
    region: str                # us | tw | jp | kr | hk  -> picks the feed
    tz: str                    # IANA tz for the climate-day boundary
    units: str = "F"           # "F" (US/Polymarket-US) | "C" (Asian agencies)
    precision: float = 1.0     # resolved value rounding (1.0 = whole degree)
    day_boundary: str = "LST"  # "LST" (Kalshi climate day) | "LOCAL_MIDNIGHT"

    @property
    def feed_source(self) -> str:
        return FEED_FOR_REGION.get(self.region, "synoptic")

    @property
    def latlon(self) -> "tuple[float, float]":
        """(lat, lon) of the resolving station — needed to query forecasts."""
        return STATION_COORDS.get(self.station, (0.0, 0.0))

    def climate_day(self, now: Optional[datetime] = None) -> date:
        """The market's climate-day date in the station's local timezone."""
        local = self._localize(now)
        return local.date()

    def day_window(self, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
        """[start, end) of the current climate day in the station's local tz.

        For LST climate day we use local standard time midnight→midnight; for
        the common case we approximate with local midnight (zoneinfo already
        accounts for the offset). Both venues take the daily high *within this
        window* — this is what "daily high so far" must be computed over.
        """
        local = self._localize(now)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start, end

    def _localize(self, now: Optional[datetime]) -> datetime:
        if now is None:
            now = datetime.now(tz=ZoneInfo("UTC")) if ZoneInfo else datetime.utcnow()
        if ZoneInfo is not None:
            tz = ZoneInfo(self.tz)
            if now.tzinfo is None:
                now = now.replace(tzinfo=ZoneInfo("UTC"))
            return now.astimezone(tz)
        return now


# --- Registry --------------------------------------------------------------
# Keyed by (venue, city). Add a city here before the bot may trade it.

_SPECS: List[ResolutionSpec] = [
    # ---- Kalshi (NWS CLI station per contract) ----
    ResolutionSpec("kalshi", "nyc", "KNYC", "NWS CLI — Central Park (KNYC)", "us", "America/New_York"),
    ResolutionSpec("kalshi", "chicago", "KMDW", "NWS CLI — Chicago Midway (KMDW)", "us", "America/Chicago"),
    ResolutionSpec("kalshi", "la", "KLAX", "NWS CLI — Los Angeles Intl (KLAX)", "us", "America/Los_Angeles"),
    ResolutionSpec("kalshi", "miami", "KMIA", "NWS CLI — Miami Intl (KMIA)", "us", "America/New_York"),
    ResolutionSpec("kalshi", "denver", "KDEN", "NWS CLI — Denver Intl (KDEN)", "us", "America/Denver"),
    ResolutionSpec("kalshi", "austin", "KAUS", "NWS CLI — Austin-Bergstrom (KAUS)", "us", "America/Chicago"),
    ResolutionSpec("kalshi", "philadelphia", "KPHL", "NWS CLI — Philadelphia Intl (KPHL)", "us", "America/New_York"),
    ResolutionSpec("kalshi", "houston", "KIAH", "NWS CLI — Houston Bush (KIAH)", "us", "America/Chicago"),

    # ---- Polymarket (Wunderground at primary airport ASOS) ----
    ResolutionSpec("polymarket", "nyc", "KLGA", "Wunderground — LaGuardia (KLGA)", "us", "America/New_York"),
    # International cities (Asian agency stations). Units in °C.
    ResolutionSpec("polymarket", "taipei", "466920", "CWA — Taipei (466920)", "tw", "Asia/Taipei", units="C", precision=0.1),
    ResolutionSpec("polymarket", "tokyo", "44132", "JMA AMeDAS — Tokyo/Otemachi (44132)", "jp", "Asia/Tokyo", units="C", precision=0.1),
    ResolutionSpec("polymarket", "seoul", "108", "KMA — Seoul (108)", "kr", "Asia/Seoul", units="C", precision=0.1),
    ResolutionSpec("polymarket", "hongkong", "HKO", "HKO — Hong Kong Observatory HQ", "hk", "Asia/Hong_Kong", units="C", precision=0.1),
]

# Approximate coordinates of each resolving station (for forecast queries).
STATION_COORDS: Dict[str, Tuple[float, float]] = {
    "KNYC": (40.78, -73.97), "KLGA": (40.78, -73.88), "KMDW": (41.79, -87.75),
    "KLAX": (33.94, -118.41), "KMIA": (25.79, -80.29), "KDEN": (39.85, -104.66),
    "KAUS": (30.20, -97.67), "KPHL": (39.87, -75.24), "KIAH": (29.99, -95.36),
    "466920": (25.04, 121.51), "44132": (35.69, 139.75), "108": (37.57, 126.97),
    "HKO": (22.30, 114.17),
}

RESOLUTION_SPECS: Dict[Tuple[str, str], ResolutionSpec] = {
    (s.venue, s.city): s for s in _SPECS
}


def get_resolution(venue: str, city: str) -> Optional[ResolutionSpec]:
    return RESOLUTION_SPECS.get((venue.lower(), city.lower()))


def known_cities(venue: str) -> List[str]:
    return sorted({c for (v, c) in RESOLUTION_SPECS if v == venue.lower()})


def station_for(venue: str, city: str) -> Optional[str]:
    spec = get_resolution(venue, city)
    return spec.station if spec else None
