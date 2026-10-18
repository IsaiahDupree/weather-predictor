"""
Risk limits — prediction-market-tuned thresholds.

Reproduces TradingBot's config-driven RiskLimits, with values appropriate for a
many-small-bets prediction-market bot (the profitability rests on the law of
large numbers across small positive-EV positions, so per-trade caps are tiny
and the daily-loss cap is the real backstop).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from src.config import Config


@dataclass
class RiskLimits:
    """Per-account risk thresholds. All monetary values in USD."""

    max_order_notional: float = 25.0            # $ cap per single order
    max_position_per_event: float = 100.0       # $ cap of exposure to one event
    max_total_exposure_frac: float = 0.5        # fraction of bankroll deployable at once
    max_orders_per_minute: int = 30
    daily_loss_cap: float = 100.0               # auto-halt threshold
    min_ev_threshold: float = 0.05              # base T before fees/spread/buffer
    vol_gate_k: float = 3.0                     # margin >= k * sigma_remaining to scalp
    liquidity_depth_frac: float = 0.33          # order <= this fraction of top-of-book depth
    max_signal_age_s: float = 120.0             # drop signals older than this (stale-signal gate)
    bankroll_usd: float = 1000.0
    forbidden_events: List[str] = field(default_factory=list)
    enabled: bool = True
    entries_enabled: bool = True

    @property
    def max_total_exposure(self) -> float:
        return self.max_total_exposure_frac * self.bankroll_usd

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RiskLimits":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_config(cls, config: "Config") -> "RiskLimits":
        return cls(
            max_order_notional=config.max_order_notional,
            max_position_per_event=config.max_position_per_event,
            max_total_exposure_frac=config.max_total_exposure_frac,
            max_orders_per_minute=config.max_orders_per_minute,
            daily_loss_cap=config.daily_loss_cap,
            min_ev_threshold=config.min_ev_threshold,
            vol_gate_k=config.vol_gate_k,
            liquidity_depth_frac=config.liquidity_depth_frac,
            bankroll_usd=config.bankroll_usd,
            entries_enabled=config.enable_entries,
        )


DEFAULT_LIMITS = RiskLimits()


def load_limits(config: Optional["Config"] = None) -> RiskLimits:
    if config is None:
        return RiskLimits()
    return RiskLimits.from_config(config)
