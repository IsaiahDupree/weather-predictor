"""
Open-Meteo Ensemble forecast client — the primary daily-Tmax *distribution*.

Source : Open-Meteo Ensemble API
Docs   : https://open-meteo.com/en/docs/ensemble-api
Host   : free       -> https://ensemble-api.open-meteo.com/v1/ensemble
         commercial -> https://customer-ensemble-api.open-meteo.com/v1/ensemble
                       (used only when an API key is configured; key passed as
                        the ``apikey`` query parameter)
Cadence: model dependent — GFS/GEFS refresh ~every 6 h, ECMWF-IFS ~every 12 h.
         Polling once an hour comfortably catches every new ensemble run.

Why ensemble: each global ensemble system runs many perturbed members, so a
single request returns a *real* sample of plausible daily-max temperatures
rather than one deterministic "vote". We pull every member from a basket of
independent multi-model ensembles (NCEP GEFS, ECMWF-IFS, DWD ICON, CMC GEM),
pool them into ``ForecastResult.members`` and set ``point`` to their mean.

Response shape (verified live 2026-05-30 against the free host)::

    {"daily": {
        "time": ["2026-05-31"],
        # control / aggregate series — NOT a member, excluded:
        "temperature_2m_max_ncep_gefs025": [74.4],
        # one series per perturbed member, suffixed by model when >1 model:
        "temperature_2m_max_member01_ncep_gefs025": [73.6],
        "temperature_2m_max_member02_ncep_gefs025": [74.3],
        ...
        "temperature_2m_max_member01_ecmwf_ifs025_ensemble": [74.8],
        ...
    }}

When a *single* model is requested the model suffix is dropped, e.g.
``temperature_2m_max_member01``. A member series is therefore exactly any
``daily`` key matching ``temperature_2m_max_member<NN>[_<model>]``; the bare
``temperature_2m_max`` / ``temperature_2m_max_<model>`` keys are control or
mean fields and are deliberately skipped so they are not double-counted.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

import httpx

from src.config import get_config
from src.data.feeds.forecast_base import ForecastClient, ForecastResult

# Multi-model ensemble basket. Each is an independent global EPS, so pooling
# their members widens the sample and de-correlates single-model bias.
_DEFAULT_MODELS = ("gfs025", "ecmwf_ifs025", "icon_seamless", "gem_global")

# Matches a perturbed-member daily series, with or without a trailing model
# suffix, e.g. "temperature_2m_max_member07" or
# "temperature_2m_max_member07_ecmwf_ifs025_ensemble". The bare control/mean
# series "temperature_2m_max[_<model>]" carry no "_memberNN" and so do not match.
_MEMBER_KEY_RE = re.compile(r"^temperature_2m_max_member\d+(?:_.+)?$")

_HTTP_TIMEOUT = 10.0


def _to_temperature_unit(units: str) -> str:
    """Map the contract's "F"/"C" onto Open-Meteo's ``temperature_unit`` value."""
    return "fahrenheit" if (units or "F").strip().upper().startswith("F") else "celsius"


class OpenMeteoForecast(ForecastClient):
    """Daily-Tmax ensemble distribution from the Open-Meteo Ensemble API."""

    name = "openmeteo_ensemble"

    def __init__(self, config=None) -> None:
        self._config = config or get_config()
        # Optional commercial key — when present we must use the customer host.
        self._api_key: str = getattr(self._config, "openmeteo_api_key", "") or ""

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        # Free, key-less host always works; availability never depends on creds.
        return True

    def _base_url(self) -> str:
        if self._api_key:
            return "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
        return "https://ensemble-api.open-meteo.com/v1/ensemble"

    # ------------------------------------------------------------------

    async def daily_tmax(
        self,
        lat: float,
        lon: float,
        target_date: date,
        units: str = "F",
    ) -> ForecastResult:
        """Return pooled ensemble daily-max temperatures for ``target_date``.

        ``ForecastResult.units`` is guaranteed to match the requested ``units``
        because we ask Open-Meteo to convert (``temperature_unit``) server-side.
        """
        out_units = "F" if (units or "F").strip().upper().startswith("F") else "C"
        iso_day = target_date.isoformat()

        params = {
            "latitude": f"{float(lat):.4f}",
            "longitude": f"{float(lon):.4f}",
            "daily": "temperature_2m_max",
            "models": ",".join(_DEFAULT_MODELS),
            "temperature_unit": _to_temperature_unit(out_units),
            "start_date": iso_day,
            "end_date": iso_day,
            "timezone": "auto",
        }
        if self._api_key:
            params["apikey"] = self._api_key

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(self._base_url(), params=params)
            resp.raise_for_status()
            payload = resp.json()

        # Open-Meteo signals soft failures with {"error": true, "reason": ...}.
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(
                f"Open-Meteo ensemble error: {payload.get('reason', 'unknown')}"
            )

        daily = (payload or {}).get("daily") or {}
        times = daily.get("time") or []
        if not times:
            return ForecastResult(
                source=self.name, target_date=iso_day, units=out_units, members=[]
            )

        # Locate the row for the requested day (we request a single day, so it is
        # index 0, but match defensively in case the API widens the window).
        try:
            idx = times.index(iso_day)
        except ValueError:
            idx = 0

        members: List[float] = []
        for key, series in daily.items():
            if key == "time" or not _MEMBER_KEY_RE.match(key):
                continue
            if not isinstance(series, list) or idx >= len(series):
                continue
            val = series[idx]
            # Skip missing / sentinel values (null, "-99", non-finite).
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if fval != fval or abs(fval) == float("inf"):  # NaN / inf guard
                continue
            if fval <= -99.0:  # Open-Meteo / station fill value
                continue
            members.append(fval)

        point: Optional[float] = (sum(members) / len(members)) if members else None

        return ForecastResult(
            source=self.name,
            target_date=iso_day,
            units=out_units,
            members=members,
            point=point,
        )
