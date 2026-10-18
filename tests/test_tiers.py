"""Integration tests for the WeatherPredictor tiered deployment ladder (src/risk/tiers.py) — proving it wires to WP's
REAL rails (RiskEngine, KillSwitch, RiskLimits) and the Config mode/live-confirm/venue gates. Everything injectable
so these run offline. Dense parametrization = many real boundary + integration cases."""
import tempfile
import pytest

from src.risk.tiers import (
    TIERS, BY_NAME, BY_LEVEL, Tier, TrackRecord, tier_by_name, next_tier, current_tier,
    can_promote, gate_order, ladder_summary, default_wp_live_gate, OrderDecision, PromotionCheck,
)
from src.risk.kill_switch import KillSwitch
from src.config import Config

LIVE_TIERS = ["LIVE_MICRO", "LIVE_SMALL", "LIVE_SCALED"]
NONLIVE = ["SIM", "PAPER"]
ALL_TIERS = NONLIVE + LIVE_TIERS
PASS = lambda: None


def _ks(tripped=False):
    ks = KillSwitch(tempfile.mktemp())
    if tripped:
        ks.trip("test")
    return ks


def _pf(equity=1000.0, total=0.0, by_event=None):
    return {"equity": equity, "exposure_total": total, "exposure_by_event": by_event or {}}


# ============================================================ ladder structure
@pytest.mark.parametrize("name,level,mode,gate", [
    ("SIM", 0, "sim", False), ("PAPER", 1, "paper", False), ("LIVE_MICRO", 2, "live", True),
    ("LIVE_SMALL", 3, "live", True), ("LIVE_SCALED", 4, "live", True),
])
def test_tier_identity(name, level, mode, gate):
    t = tier_by_name(name)
    assert t.level == level and t.mode == mode and t.requires_live_gate is gate
    assert BY_LEVEL[level] is t and BY_NAME[name] is t


@pytest.mark.parametrize("name", ALL_TIERS)
def test_tier_name_case_insensitive(name):
    assert tier_by_name(name.lower()) is tier_by_name(name.upper())


@pytest.mark.parametrize("bad", ["", "LIVE", "x", "tier"])
def test_invalid_tier_raises(bad):
    with pytest.raises(ValueError):
        tier_by_name(bad)


@pytest.mark.parametrize("frm,to", [("SIM", "PAPER"), ("PAPER", "LIVE_MICRO"), ("LIVE_MICRO", "LIVE_SMALL"), ("LIVE_SMALL", "LIVE_SCALED")])
def test_next_tier(frm, to):
    assert next_tier(tier_by_name(frm)).name == to


def test_top_tier_no_next():
    assert next_tier(tier_by_name("LIVE_SCALED")) is None


@pytest.mark.parametrize("env,expect", [
    ({}, "SIM"), ({"WP_TIER": "PAPER"}, "PAPER"), ({"WP_TIER": "live_micro"}, "LIVE_MICRO"),
    ({"WP_TIER": "LIVE_SCALED"}, "LIVE_SCALED"),
])
def test_current_tier_env(env, expect):
    assert current_tier(env).name == expect


def test_current_tier_default_is_sim_not_live():
    assert current_tier({}).mode == "sim"


@pytest.mark.parametrize("lo,hi", [(2, 3), (3, 4)])
def test_live_caps_increase(lo, hi):
    a, b = BY_LEVEL[lo].limits, BY_LEVEL[hi].limits
    assert b.max_order_notional >= a.max_order_notional and b.daily_loss_cap >= a.daily_loss_cap and b.bankroll_usd >= a.bankroll_usd


# ============================================================ non-live tiers place orders (no live gate)
@pytest.mark.parametrize("tier", NONLIVE)
@pytest.mark.parametrize("stake,price", [(1, 0.5), (10, 0.4), (24, 0.8)])
def test_nonlive_valid_order_allowed(tier, stake, price):
    t = tier_by_name(tier)
    d = gate_order(t, stake=stake, qty=stake / price, price=price, portfolio=_pf(), kill_switch=_ks())
    assert d.allow is True and d.code == "ok" and d.mode == t.mode


@pytest.mark.parametrize("tier", NONLIVE)
def test_nonlive_skips_live_gate(tier):
    # a non-live tier never invokes the (would-raise) live gate even with no config
    t = tier_by_name(tier)
    d = gate_order(t, stake=1, qty=2, price=0.5, portfolio=_pf(), kill_switch=_ks(), config=None)
    assert d.allow is True


# ============================================================ Config live-gate integration (WP-specific)
@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_sim_config_blocks_live_tier(tier):
    d = gate_order(tier_by_name(tier), stake=1, qty=2, price=0.4, portfolio=_pf(equity=5),
                   kill_switch=_ks(), config=Config(mode="sim"))
    assert d.allow is False and d.code == "LIVE_GATE_BLOCKED"


@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_live_config_without_confirm_blocks_not_crashes(tier):
    # live mode but WP_LIVE_CONFIRM unset -> assert_runnable raises RuntimeError -> normalized to LIVE_GATE_BLOCKED
    d = gate_order(tier_by_name(tier), stake=1, qty=2, price=0.4, portfolio=_pf(equity=5),
                   kill_switch=_ks(), config=Config(mode="live", live_confirm=""))
    assert d.allow is False and d.code == "LIVE_GATE_BLOCKED"


@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_live_config_confirmed_passes_gate(tier):
    # live + confirmed + default kalshi venue -> gate passes -> RiskEngine runs (valid order ok)
    t = tier_by_name(tier)
    notional = t.limits.max_order_notional * 0.5
    d = gate_order(t, stake=notional, qty=notional / 0.4, price=0.4, portfolio=_pf(equity=t.limits.bankroll_usd),
                   kill_switch=_ks(), config=Config(mode="live", live_confirm="I_UNDERSTAND"))
    assert d.allow is True and d.code == "ok"


def test_default_wp_live_gate_none_config_raises():
    with pytest.raises(PermissionError):
        default_wp_live_gate(None)


def test_default_wp_live_gate_polymarket_venue_needs_ack():
    # Config is frozen -> construct with the venue fields. PM live without the non-US-person ack is blocked.
    cfg = Config(mode="live", live_confirm="I_UNDERSTAND", default_venue="polymarket",
                 enable_polymarket=True, polymarket_legal_ack="")
    with pytest.raises(PermissionError):
        default_wp_live_gate(cfg)


# ============================================================ injected gate + stake cap + kill switch
@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_injected_gate_block(tier):
    def blocked():
        raise PermissionError("operator not armed")
    t = tier_by_name(tier)
    d = gate_order(t, stake=t.limits.max_order_notional, qty=1, price=0.5,
                   portfolio=_pf(equity=t.limits.bankroll_usd), kill_switch=_ks(), live_gate=blocked)
    assert d.allow is False and d.code == "LIVE_GATE_BLOCKED" and "armed" in d.reason


@pytest.mark.parametrize("tier", LIVE_TIERS)
@pytest.mark.parametrize("mult,code", [(0.5, "ok"), (1.0, "ok"), (1.001, "STAKE_OVER_TIER"), (3.0, "STAKE_OVER_TIER")])
def test_stake_cap(tier, mult, code):
    t = tier_by_name(tier)
    stake = t.limits.max_order_notional * mult
    d = gate_order(t, stake=stake, qty=max(stake, 0.01) / 0.5, price=0.5,
                   portfolio=_pf(equity=t.limits.bankroll_usd), kill_switch=_ks(), live_gate=PASS)
    assert d.code == code


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_kill_switch_halts_every_tier(tier):
    t = tier_by_name(tier)
    kw = dict(live_gate=PASS) if t.requires_live_gate else {}
    d = gate_order(t, stake=min(t.limits.max_order_notional, 1.0) or 1.0, qty=1, price=0.5,
                   portfolio=_pf(equity=max(t.limits.bankroll_usd, 5)), kill_switch=_ks(tripped=True), **kw)
    # SIM/PAPER caps allow $1; the RiskEngine HALTED gate fires before notional regardless
    assert d.allow is False and d.code == "HALTED"


# ============================================================ RiskEngine reject codes surface through the tier
@pytest.mark.parametrize("tier", LIVE_TIERS)
@pytest.mark.parametrize("price", [0.0, -0.1, 1.001])
def test_invalid_price(tier, price):
    t = tier_by_name(tier)
    d = gate_order(t, stake=0.5, qty=1, price=price, portfolio=_pf(equity=t.limits.bankroll_usd),
                   kill_switch=_ks(), live_gate=PASS)
    assert d.allow is False and d.code == "INVALID_PRICE"


@pytest.mark.parametrize("tier", LIVE_TIERS)
@pytest.mark.parametrize("qty", [0.0, -1.0])
def test_invalid_qty(tier, qty):
    t = tier_by_name(tier)
    d = gate_order(t, stake=0.5, qty=qty, price=0.5, portfolio=_pf(equity=t.limits.bankroll_usd),
                   kill_switch=_ks(), live_gate=PASS)
    assert d.allow is False and d.code == "INVALID_QTY"


@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_order_notional_cap(tier):
    t = tier_by_name(tier)
    price = 0.5
    over_qty = (t.limits.max_order_notional + 1.0) / price
    d = gate_order(t, stake=t.limits.max_order_notional, qty=over_qty, price=price,
                   portfolio=_pf(equity=t.limits.bankroll_usd), kill_switch=_ks(), live_gate=PASS)
    assert d.allow is False and d.code == "ORDER_NOTIONAL"


@pytest.mark.parametrize("tier", LIVE_TIERS)
def test_total_exposure_cap(tier):
    t = tier_by_name(tier)
    d = gate_order(t, stake=0.5, qty=1, price=0.5,
                   portfolio=_pf(equity=t.limits.bankroll_usd, total=t.limits.max_total_exposure + 5),
                   kill_switch=_ks(), live_gate=PASS)
    assert d.allow is False and d.code in ("TOTAL_EXPOSURE", "EVENT_CONCENTRATION")


# ============================================================ promotion (track-record earned)
@pytest.mark.parametrize("tier", ["SIM", "PAPER", "LIVE_MICRO", "LIVE_SMALL"])
@pytest.mark.parametrize("delta,expect", [(-1, False), (0, True), (5, True)])
def test_promote_settled_boundary(tier, delta, expect):
    t = tier_by_name(tier)
    tr = TrackRecord(settled_trades=t.min_settled + delta, net_pnl=1.0, days_live=t.min_days + 1)
    assert can_promote(t, tr).gates["settled_trades"] is expect


@pytest.mark.parametrize("tier", ["SIM", "PAPER", "LIVE_MICRO", "LIVE_SMALL"])
@pytest.mark.parametrize("delta,expect", [(-0.01, False), (0.0, True), (1.0, True)])
def test_promote_pnl_boundary(tier, delta, expect):
    t = tier_by_name(tier)
    tr = TrackRecord(settled_trades=t.min_settled + 1, net_pnl=t.min_net_pnl + delta, days_live=t.min_days + 1)
    assert can_promote(t, tr).gates["net_pnl"] is expect


@pytest.mark.parametrize("tier", ["PAPER", "LIVE_MICRO", "LIVE_SMALL"])
@pytest.mark.parametrize("frac,expect", [(0.99, True), (1.0, True), (1.01, False)])
def test_promote_drawdown_boundary(tier, frac, expect):
    t = tier_by_name(tier)
    dd_cap = t.max_drawdown_frac * t.limits.bankroll_usd
    tr = TrackRecord(settled_trades=t.min_settled + 1, net_pnl=1.0, days_live=t.min_days + 1, max_drawdown=dd_cap * frac)
    assert can_promote(t, tr).gates["drawdown_ok"] is expect


@pytest.mark.parametrize("tier", ["SIM", "PAPER", "LIVE_MICRO", "LIVE_SMALL"])
def test_promote_all_pass(tier):
    t = tier_by_name(tier)
    pc = can_promote(t, TrackRecord(settled_trades=t.min_settled, net_pnl=t.min_net_pnl, days_live=t.min_days, max_drawdown=0.0))
    assert pc.can_promote is True and pc.to_tier == next_tier(t).name


def test_top_tier_never_promotes():
    pc = can_promote(tier_by_name("LIVE_SCALED"), TrackRecord(settled_trades=10**6, net_pnl=10**6, days_live=10**6))
    assert pc.can_promote is False and pc.to_tier is None


def test_full_promotion_journey():
    t = tier_by_name("SIM")
    path = [t.name]
    track = TrackRecord(settled_trades=10**6, net_pnl=10**6, days_live=10**6, max_drawdown=0.0)
    while True:
        pc = can_promote(t, track)
        if not pc.can_promote:
            break
        t = tier_by_name(pc.to_tier)
        path.append(t.name)
    assert path == ["SIM", "PAPER", "LIVE_MICRO", "LIVE_SMALL", "LIVE_SCALED"]


def test_ladder_summary_shape():
    s = ladder_summary()
    assert len(s) == 5 and s[0]["tier"] == "SIM" and s[-1]["tier"] == "LIVE_SCALED"


def test_decision_and_promo_dataclasses():
    d = gate_order(tier_by_name("SIM"), stake=1, qty=2, price=0.5, portfolio=_pf(), kill_switch=_ks())
    assert isinstance(d, OrderDecision)
    pc = can_promote(tier_by_name("SIM"), TrackRecord())
    assert isinstance(pc, PromotionCheck)
