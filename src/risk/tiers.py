"""
src/risk/tiers.py — the TIERED deployment ladder for WeatherPredictor, integrated with WP's own rails.

The rails (RiskLimits / RiskEngine / KillSwitch) and the mode system (sim/paper/live, Config.live_confirm) already
exist; this wraps them into an explicit, track-record-earned promotion ladder so capital scales only as the forward
results justify it — the same discipline the trading research program proved out and is folding back here:

    SIM  ->  PAPER  ->  LIVE_MICRO  ->  LIVE_SMALL  ->  LIVE_SCALED

Each tier carries its OWN RiskLimits (per-order/daily-loss/bankroll caps) and its OWN promotion criteria (min settled
trades, min net PnL, min days, max drawdown). WP places orders in EVERY mode (to the sim/paper/live adapter), so the
per-tier RiskEngine gates every order; LIVE tiers additionally require the live gate (Config.assert_runnable +
live_confirm + venue-allowed + kill-switch clear). Promotion is earned, never assumed.

Everything is injectable (the live gate, the kill switch) so it is fully unit-testable offline against WP's real
RiskEngine. Pure logic; no network, no adapter I/O.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .limits import RiskLimits
from .risk_engine import RiskEngine
from .kill_switch import KillSwitch


# ---------------------------------------------------------------- track record (drives promotion)
@dataclass
class TrackRecord:
    settled_trades: int = 0
    net_pnl: float = 0.0
    days_live: float = 0.0
    max_drawdown: float = 0.0      # worst peak-to-trough, a POSITIVE dollar amount
    win_rate: float = 0.0


# ---------------------------------------------------------------- a tier
@dataclass
class Tier:
    name: str
    level: int
    mode: str                      # "sim" | "paper" | "live"  (matches WP_MODE / VALID_MODES)
    limits: RiskLimits
    requires_live_gate: bool
    # criteria to PROMOTE out of this tier into the next one:
    min_settled: int = 0
    min_net_pnl: float = 0.0
    min_days: float = 0.0
    max_drawdown_frac: float = 1.0  # max allowed drawdown as a fraction of THIS tier's bankroll


# The ladder. Caps widen only as the track record earns it. SIM/PAPER use a notional bankroll for realistic sizing;
# the LIVE tiers start tiny ($1 micro) and step up.
TIERS: List[Tier] = [
    Tier("SIM", 0, "sim",
         RiskLimits(max_order_notional=25.0, max_position_per_event=100.0, daily_loss_cap=100.0, bankroll_usd=1000.0),
         requires_live_gate=False, min_settled=100, min_net_pnl=0.0, min_days=1.0, max_drawdown_frac=1.0),
    Tier("PAPER", 1, "paper",
         RiskLimits(max_order_notional=25.0, max_position_per_event=100.0, daily_loss_cap=100.0, bankroll_usd=1000.0),
         requires_live_gate=False, min_settled=300, min_net_pnl=0.0, min_days=3.0, max_drawdown_frac=0.5),
    Tier("LIVE_MICRO", 2, "live",
         RiskLimits(max_order_notional=1.0, max_position_per_event=2.0, daily_loss_cap=2.0,
                    max_total_exposure_frac=0.5, bankroll_usd=5.0),
         requires_live_gate=True, min_settled=50, min_net_pnl=0.0, min_days=3.0, max_drawdown_frac=0.30),
    Tier("LIVE_SMALL", 3, "live",
         RiskLimits(max_order_notional=5.0, max_position_per_event=25.0, daily_loss_cap=10.0,
                    max_total_exposure_frac=0.25, bankroll_usd=50.0),
         requires_live_gate=True, min_settled=200, min_net_pnl=0.0, min_days=7.0, max_drawdown_frac=0.20),
    Tier("LIVE_SCALED", 4, "live",
         RiskLimits(max_order_notional=25.0, max_position_per_event=100.0, daily_loss_cap=100.0,
                    max_total_exposure_frac=0.25, bankroll_usd=500.0),
         requires_live_gate=True, min_settled=1000, min_net_pnl=0.0, min_days=21.0, max_drawdown_frac=0.15),
]
BY_NAME: Dict[str, Tier] = {t.name: t for t in TIERS}
BY_LEVEL: Dict[int, Tier] = {t.level: t for t in TIERS}


def tier_by_name(name: str) -> Tier:
    t = BY_NAME.get(str(name).upper())
    if t is None:
        raise ValueError(f"unknown tier {name!r}; valid: {list(BY_NAME)}")
    return t


def next_tier(tier: Tier) -> Optional[Tier]:
    return BY_LEVEL.get(tier.level + 1)


def current_tier(env: Optional[Dict[str, str]] = None) -> Tier:
    """Resolve the active tier from WP_TIER (default SIM — live is never the default)."""
    env = env if env is not None else os.environ
    return tier_by_name(env.get("WP_TIER", "SIM"))


# ---------------------------------------------------------------- the WP live gate (config-backed)
def default_wp_live_gate(config) -> None:
    """WP's live preconditions, raising PermissionError unless ALL hold: runnable config, live mode confirmed
    (WP_LIVE_CONFIRM=I_UNDERSTAND), and the venue is permitted (Polymarket needs the legality ack). The kill switch
    is checked separately by the RiskEngine. Pass a Config; tests inject a lambda instead."""
    if config is None:
        raise PermissionError("no Config supplied for the live gate")
    try:
        config.assert_runnable()                  # raises RuntimeError/ValueError on unsafe config (incl. live_confirm)
    except (RuntimeError, ValueError) as e:
        raise PermissionError(f"config not runnable: {e}")   # normalize to the gate's PermissionError contract
    if not config.is_live():
        raise PermissionError("config mode is not 'live'")
    if getattr(config, "default_venue", "kalshi") == "polymarket" and not config.polymarket_allowed():
        raise PermissionError("Polymarket venue not permitted (enable + non-US-person legality ack required)")


# ---------------------------------------------------------------- promotion
@dataclass
class PromotionCheck:
    can_promote: bool
    from_tier: str
    to_tier: Optional[str]
    gates: Dict[str, bool]
    failing: List[str]
    reason: str


def can_promote(tier: Tier, track: TrackRecord) -> PromotionCheck:
    """Has the lane earned the NEXT tier? Every gate must pass. The top tier never promotes."""
    nxt = next_tier(tier)
    if nxt is None:
        return PromotionCheck(False, tier.name, None, {}, [], "already at the top tier")
    dd_cap = tier.max_drawdown_frac * tier.limits.bankroll_usd
    gates = {
        "settled_trades": track.settled_trades >= tier.min_settled,
        "net_pnl": track.net_pnl >= tier.min_net_pnl,
        "days_live": track.days_live >= tier.min_days,
        "drawdown_ok": track.max_drawdown <= dd_cap,
    }
    failing = [k for k, ok in gates.items() if not ok]
    can = not failing
    reason = (f"promote {tier.name} -> {nxt.name}" if can else f"stay at {tier.name}; failing: {', '.join(failing)}")
    return PromotionCheck(can, tier.name, nxt.name, gates, failing, reason)


# ---------------------------------------------------------------- the order gate (the single entry point)
@dataclass
class OrderDecision:
    allow: bool
    tier: str
    mode: str
    code: str           # "ok" | "STAKE_OVER_TIER" | "LIVE_GATE_BLOCKED" | <RiskEngine reject code>
    reason: str


def gate_order(tier: Tier, *, stake: float, qty: float, price: float, portfolio: Dict,
               event_ticker: Optional[str] = None, config=None, kill_switch: Optional[KillSwitch] = None,
               live_gate: Optional[Callable[..., None]] = None, **risk_kw) -> OrderDecision:
    """The unified gate. WP routes orders in EVERY mode (to the sim/paper/live adapter), so the per-tier RiskEngine
    gates every order; LIVE tiers additionally require the live gate. `live_gate` and `kill_switch` are injectable for
    tests (the default live gate reads the Config)."""
    if stake > tier.limits.max_order_notional + 1e-9:
        return OrderDecision(False, tier.name, tier.mode, "STAKE_OVER_TIER",
                             f"stake ${stake:.2f} exceeds {tier.name} per-order cap ${tier.limits.max_order_notional:.2f}")

    if tier.requires_live_gate:
        gate = live_gate if live_gate is not None else (lambda: default_wp_live_gate(config))
        try:
            gate()
        except PermissionError as e:
            return OrderDecision(False, tier.name, tier.mode, "LIVE_GATE_BLOCKED", str(e))

    engine = RiskEngine(limits=tier.limits, kill_switch=kill_switch)
    rc = engine.check(symbol=event_ticker or "wx", qty=qty, price=price, portfolio=portfolio,
                      event_ticker=event_ticker, **risk_kw)
    if not rc.ok:
        return OrderDecision(False, tier.name, tier.mode, rc.code, rc.message)
    return OrderDecision(True, tier.name, tier.mode, "ok", "approved by tier rails")


def ladder_summary() -> List[Dict]:
    out = []
    for t in TIERS:
        out.append({"tier": t.name, "level": t.level, "mode": t.mode,
                    "max_order_$": t.limits.max_order_notional, "daily_loss_cap_$": t.limits.daily_loss_cap,
                    "bankroll_$": t.limits.bankroll_usd, "requires_live_gate": t.requires_live_gate,
                    "promote_when": {"settled>=": t.min_settled, "net_pnl>=": t.min_net_pnl,
                                     "days>=": t.min_days, "max_dd<=$": round(t.max_drawdown_frac * t.limits.bankroll_usd, 2)}})
    return out
