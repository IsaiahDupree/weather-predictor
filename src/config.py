"""
Central configuration for WeatherPredictor.

Everything that varies between runs (mode, venue, cities, bankroll, risk
thresholds, API credentials) is read from the environment exactly once into a
frozen ``Config`` object. No secret ever appears in code or logs.

Load order:
  1. process environment (os.environ)
  2. a local .env file if python-dotenv is installed (never committed)

Nothing here imports other ``src`` modules — this is a Tier-0 primitive so it
can be imported from anywhere without a cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Load .env once, if available. Absent dotenv is fine — env vars still work.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _env(name)
    if not raw:
        return list(default or [])
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Valid run modes, in promotion order.
VALID_MODES = ("sim", "paper", "live")
VALID_VENUES = ("kalshi", "polymarket")


@dataclass(frozen=True)
class Config:
    """Immutable run configuration. Build with ``Config.load()``."""

    # ---- Runtime ----
    mode: str = "sim"                      # sim | paper | live
    live_confirm: str = ""                 # must == "I_UNDERSTAND" to run live
    bankroll_usd: float = 1000.0
    cities: List[str] = field(default_factory=lambda: ["nyc"])
    default_venue: str = "kalshi"
    enable_entries: bool = True

    # ---- Venue gating ----
    enable_polymarket: bool = False
    polymarket_legal_ack: str = ""         # must == "NON_US_PERSON" to enable PM live

    # ---- Risk limits (override RiskLimits defaults) ----
    max_order_notional: float = 25.0
    max_position_per_event: float = 100.0
    max_total_exposure_frac: float = 0.5
    max_orders_per_minute: int = 30
    daily_loss_cap_usd: float = 0.0        # 0 => derived as 10% of bankroll
    min_ev_threshold: float = 0.05
    vol_gate_k: float = 3.0
    liquidity_depth_frac: float = 0.33

    # ---- Kalshi ----
    kalshi_env: str = "demo"               # demo | prod
    kalshi_api_key_id: str = ""
    kalshi_private_key_pem_path: str = ""

    # ---- Polymarket ----
    polymarket_pk: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 1
    polymarket_chain_id: int = 137

    # ---- Weather: raw obs ----
    synoptic_token: str = ""
    iem_user_agent: str = "weatherpredictor/1.0 (set IEM_USER_AGENT)"
    nws_user_agent: str = "weatherpredictor/1.0 (set NWS_USER_AGENT)"
    cwa_auth_key: str = ""
    kma_auth_key: str = ""

    # ---- Weather: forecast ----
    openmeteo_api_key: str = ""
    openmeteo_host: str = "https://api.open-meteo.com"
    pirate_weather_api_key: str = ""
    tomorrow_io_api_key: str = ""
    openweathermap_api_key: str = ""

    # ---- Weather: final / backtest ----
    ncei_cdo_token: str = ""
    visualcrossing_api_key: str = ""

    # ---- Misc ----
    log_dir: Path = field(default_factory=lambda: _REPO_ROOT / "data")
    kill_switch_file: Path = field(default_factory=lambda: _REPO_ROOT / "data" / "KILL_SWITCH")

    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "Config":
        mode = _env("WP_MODE", "sim").lower()
        if mode not in VALID_MODES:
            mode = "sim"
        venue = _env("WP_DEFAULT_VENUE", "kalshi").lower()
        if venue not in VALID_VENUES:
            venue = "kalshi"
        log_dir = Path(_env("WP_LOG_DIR") or (_REPO_ROOT / "data"))
        kill = Path(_env("WP_KILL_SWITCH_FILE") or (log_dir / "KILL_SWITCH"))
        return cls(
            mode=mode,
            live_confirm=_env("WP_LIVE_CONFIRM"),
            bankroll_usd=_env_float("WP_BANKROLL_USD", 1000.0),
            cities=_env_list("WP_CITIES", ["nyc"]),
            default_venue=venue,
            enable_entries=_env_bool("WP_ENABLE_ENTRIES", True),
            enable_polymarket=_env_bool("WP_ENABLE_POLYMARKET", False),
            polymarket_legal_ack=_env("WP_POLYMARKET_LEGAL_ACK"),
            max_order_notional=_env_float("WP_MAX_ORDER_NOTIONAL", 25.0),
            max_position_per_event=_env_float("WP_MAX_POSITION_PER_EVENT", 100.0),
            max_total_exposure_frac=_env_float("WP_MAX_TOTAL_EXPOSURE_FRAC", 0.5),
            max_orders_per_minute=_env_int("WP_MAX_ORDERS_PER_MINUTE", 30),
            daily_loss_cap_usd=_env_float("WP_DAILY_LOSS_CAP_USD", 0.0),
            min_ev_threshold=_env_float("WP_MIN_EV_THRESHOLD", 0.05),
            vol_gate_k=_env_float("WP_VOL_GATE_K", 3.0),
            liquidity_depth_frac=_env_float("WP_LIQUIDITY_DEPTH_FRAC", 0.33),
            kalshi_env=_env("KALSHI_ENV", "demo").lower(),
            kalshi_api_key_id=_env("KALSHI_API_KEY_ID"),
            kalshi_private_key_pem_path=_env("KALSHI_PRIVATE_KEY_PEM_PATH"),
            polymarket_pk=_env("POLYMARKET_PK"),
            polymarket_funder=_env("POLYMARKET_FUNDER"),
            polymarket_signature_type=_env_int("POLYMARKET_SIGNATURE_TYPE", 1),
            polymarket_chain_id=_env_int("POLYMARKET_CHAIN_ID", 137),
            synoptic_token=_env("SYNOPTIC_TOKEN"),
            iem_user_agent=_env("IEM_USER_AGENT") or "weatherpredictor/1.0 (set IEM_USER_AGENT)",
            nws_user_agent=_env("NWS_USER_AGENT") or "weatherpredictor/1.0 (set NWS_USER_AGENT)",
            cwa_auth_key=_env("CWA_AUTH_KEY"),
            kma_auth_key=_env("KMA_AUTH_KEY"),
            openmeteo_api_key=_env("OPENMETEO_API_KEY"),
            openmeteo_host=_env("OPENMETEO_HOST") or "https://api.open-meteo.com",
            pirate_weather_api_key=_env("PIRATE_WEATHER_API_KEY"),
            tomorrow_io_api_key=_env("TOMORROW_IO_API_KEY"),
            openweathermap_api_key=_env("OPENWEATHERMAP_API_KEY"),
            ncei_cdo_token=_env("NCEI_CDO_TOKEN"),
            visualcrossing_api_key=_env("VISUALCROSSING_API_KEY"),
            log_dir=log_dir,
            kill_switch_file=kill,
        )

    # ------------------------------------------------------------------

    @property
    def daily_loss_cap(self) -> float:
        """Daily-loss cap in $; defaults to 10% of bankroll when unset."""
        if self.daily_loss_cap_usd > 0:
            return self.daily_loss_cap_usd
        return round(0.10 * self.bankroll_usd, 2)

    def is_live(self) -> bool:
        return self.mode == "live"

    def polymarket_allowed(self) -> bool:
        """True only when PM is explicitly enabled AND the legality ack is set.

        Polymarket is blocked to US persons; the adapter stays disabled unless
        the operator both opts in and asserts non-US-person status. For LIVE
        mode this is enforced; SIM/PAPER may read public PM prices for research.
        """
        if not self.enable_polymarket:
            return False
        if self.is_live() and self.polymarket_legal_ack != "NON_US_PERSON":
            return False
        return True

    def assert_runnable(self) -> None:
        """Fail fast on an unsafe configuration before any order can fire."""
        if self.mode not in VALID_MODES:
            raise ValueError(f"WP_MODE must be one of {VALID_MODES}, got {self.mode!r}")
        if self.is_live() and self.live_confirm != "I_UNDERSTAND":
            raise RuntimeError(
                "Refusing to start in LIVE mode: set WP_LIVE_CONFIRM=I_UNDERSTAND "
                "to confirm you intend to trade real money."
            )
        if self.bankroll_usd <= 0:
            raise ValueError("WP_BANKROLL_USD must be positive for sizing math.")

    def redacted(self) -> dict:
        """Config snapshot safe to log — every credential masked."""
        secret_fields = {
            "kalshi_api_key_id", "kalshi_private_key_pem_path", "polymarket_pk",
            "polymarket_funder", "synoptic_token", "cwa_auth_key", "kma_auth_key",
            "openmeteo_api_key", "pirate_weather_api_key", "tomorrow_io_api_key",
            "openweathermap_api_key", "ncei_cdo_token", "visualcrossing_api_key",
            "live_confirm", "polymarket_legal_ack",
        }
        out = {}
        for k, v in self.__dict__.items():
            if k in secret_fields:
                out[k] = "***set***" if v else ""
            elif isinstance(v, Path):
                out[k] = str(v)
            else:
                out[k] = v
        return out


_default_config: Optional[Config] = None


def get_config() -> Config:
    """Process-wide singleton config (loaded from env on first call)."""
    global _default_config
    if _default_config is None:
        _default_config = Config.load()
    return _default_config


def reset_config() -> None:
    """Drop the cached config so the next get_config() re-reads env (tests)."""
    global _default_config
    _default_config = None
