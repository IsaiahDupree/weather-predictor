"""SimAdapter fill+settlement and the risk-gated router."""

from pathlib import Path

import pytest

from src.execution.adapters.sim import SimAdapter
from src.execution.order import Mode, Order, Outcome, Side
from src.execution.router import ExecutionRouter
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits
from src.risk.risk_engine import RiskEngine


def _router(tmp_path, **limit_overrides):
    sim = SimAdapter(starting_cash=1000.0)
    ks = KillSwitch(tmp_path / "kill")
    ks.reset()
    limits = RiskLimits(bankroll_usd=1000, daily_loss_cap=100, **limit_overrides)
    eng = RiskEngine(limits=limits, kill_switch=ks)
    r = ExecutionRouter(eng, event_log_path=tmp_path / "events.jsonl")
    r.register_adapter(sim, venue="sim")
    return r, sim, ks


async def test_fill_then_resolve_pays_one(tmp_path):
    r, sim, _ = _router(tmp_path)
    o = Order(symbol="KXHIGHNY-B71", side="buy", qty=10, limit_price=0.35,
              outcome="yes", mode=Mode.SIM, broker="sim")
    o = await r.submit(o, ref_price=0.35, event_ticker="KXHIGHNY")
    assert o.status.value == "filled" and abs(sim.get_cash() - 996.5) < 1e-9
    rec = await sim.resolve("KXHIGHNY-B71", won=True)
    assert abs(rec.pnl - 6.5) < 1e-9 and abs(sim.get_cash() - 1006.5) < 1e-9


async def test_resolve_loss_zero_payout(tmp_path):
    r, sim, _ = _router(tmp_path)
    o = Order(symbol="X", side="buy", qty=10, limit_price=0.20, mode=Mode.SIM, broker="sim")
    await r.submit(o, ref_price=0.20, event_ticker="E")
    rec = await sim.resolve("X", won=False)
    assert abs(rec.pnl - (-2.0)) < 1e-9 and abs(sim.get_cash() - 998.0) < 1e-9


async def test_order_notional_gate(tmp_path):
    r, _, _ = _router(tmp_path, max_order_notional=25)
    o = Order(symbol="X", side="buy", qty=100, limit_price=0.50, mode=Mode.SIM, broker="sim")
    o = await r.submit(o, ref_price=0.50, event_ticker="E")
    assert o.status.value == "rejected" and o.metadata["risk_code"] == "ORDER_NOTIONAL"


async def test_stale_signal_gate(tmp_path):
    r, _, _ = _router(tmp_path)
    o = Order(symbol="X", side="buy", qty=1, limit_price=0.5, mode=Mode.SIM, broker="sim")
    o = await r.submit(o, ref_price=0.5, signal_age_s=999)
    assert o.metadata["risk_code"] == "STALE_SIGNAL"


async def test_vol_gate_rejects_unless_arbitrage(tmp_path):
    r, _, _ = _router(tmp_path)
    o = Order(symbol="X", side="buy", qty=1, limit_price=0.96, mode=Mode.SIM, broker="sim")
    o = await r.submit(o, ref_price=0.96, vol_gate_pass=False)
    assert o.metadata["risk_code"] == "VOL_GATE"
    # arbitrage is exempt
    o2 = Order(symbol="Y", side="buy", qty=1, limit_price=0.30, mode=Mode.SIM, broker="sim")
    o2 = await r.submit(o2, ref_price=0.30, vol_gate_pass=False, is_arbitrage=True)
    assert o2.status.value == "filled"


async def test_event_concentration_gate(tmp_path):
    r, _, _ = _router(tmp_path, max_position_per_event=5.0, max_order_notional=100)
    # build existing exposure on the event by filling a position first
    a = Order(symbol="E-A", side="buy", qty=10, limit_price=0.40, mode=Mode.SIM, broker="sim")
    await r.submit(a, ref_price=0.40, event_ticker="E")        # $4 exposure
    b = Order(symbol="E-B", side="buy", qty=10, limit_price=0.40, mode=Mode.SIM, broker="sim")
    b = await r.submit(b, ref_price=0.40, event_ticker="E")    # +$4 -> $8 > $5 cap
    assert b.metadata["risk_code"] == "EVENT_CONCENTRATION"


async def test_kill_switch_halts(tmp_path):
    r, _, ks = _router(tmp_path)
    ks.trip("manual test halt")
    o = Order(symbol="X", side="buy", qty=1, limit_price=0.5, mode=Mode.SIM, broker="sim")
    o = await r.submit(o, ref_price=0.5)
    assert o.status.value == "rejected" and o.metadata["risk_code"] == "HALTED"


async def test_idempotency_dedup(tmp_path):
    r, sim, _ = _router(tmp_path)
    o = Order(symbol="X", side="buy", qty=1, limit_price=0.5, mode=Mode.SIM, broker="sim")
    first = await r.submit(o, ref_price=0.5)
    again = await r.submit(o, ref_price=0.5)         # same client_order_id
    assert first is again                            # returns the same order, no double-fill


async def test_entries_can_be_disabled_without_blocking_sells(tmp_path):
    r, sim, _ = _router(tmp_path, entries_enabled=False, max_order_notional=2)

    buy = Order(symbol="X", side=Side.BUY, qty=1, limit_price=0.5, mode=Mode.SIM, broker="sim")
    buy = await r.submit(buy, ref_price=0.5, event_ticker="E")
    assert buy.status.value == "rejected"
    assert buy.metadata["risk_code"] == "ENTRY_DISABLED"

    seed = Order(symbol="X", side=Side.BUY, qty=10, limit_price=0.2, mode=Mode.SIM, broker="sim")
    sim._fill(seed, 0.2)
    sell = Order(symbol="X", side=Side.SELL, qty=10, limit_price=0.8, mode=Mode.SIM, broker="sim")
    sell = await r.submit(sell, ref_price=0.8, event_ticker="E", resolution_known=False, vol_gate_pass=False)
    assert sell.status.value == "filled"
    assert abs(sim.realized_pnl() - 6.0) < 1e-9
