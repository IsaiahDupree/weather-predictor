"""Tests for the funnel planner + gas sizing. No mocks.

Pure tests run offline. The `live` test reproduces the exact bug we hit in
production (an 80k hardcoded gas limit failing the USDC.e→onramp approve) and
guards against re-introducing any flat cap below the real requirement.
"""
import pytest
import polymarket_v2 as pv
import poly_v2_send as snd

TGT = pv.to_units(5)  # 5_000_000


def _online() -> bool:
    try:
        pv.matic_balance("0x0000000000000000000000000000000000000000")
        return True
    except pv.ChainError:
        return False


live = pytest.mark.skipif(not _online(), reason="Polygon RPC unreachable")
FUNDER = "0xEE2D0C138E3CBf7965f6F9e6D63e6EC81007B482"


def test_gas_with_buffer_adds_headroom():
    assert snd.gas_with_buffer(80_000) == 104_000        # 30% over
    assert snd.gas_with_buffer(80_979) == 105_272        # the real approve estimate + buffer
    assert snd.gas_with_buffer(100, pct=0) == 100
    with pytest.raises(ValueError):
        snd.gas_with_buffer(0)


def test_buffer_clears_the_real_approve_requirement():
    # The bug: estimate ~80_979 but limit was 80_000. Buffered limit must exceed it.
    assert snd.gas_with_buffer(80_979) > 80_979


@live
def test_usdce_onramp_approve_needs_more_than_80k_gas():
    # REGRESSION GUARD for the failed tx: this approve estimates >80k, so a flat
    # 80k cap (what we had) is guaranteed to fail. Dynamic sizing is mandatory.
    est = pv.estimate_gas(pv.USDCE, pv.cd_approve(pv.ONRAMP, snd.MAX_UINT), FUNDER)
    assert est is not None, "approve should be estimable (it eth_call-succeeds)"
    assert est > 80_000, f"approve needs {est} gas — a flat 80k limit fails it"
    assert snd.gas_with_buffer(est) > est


def _state(native=0, usdce=0, pusd=0, onramp=0, exch=0, ctf=False):
    return {
        "native": native, "usdce": usdce, "pusd": pusd, "matic": 0,
        "router_allow_native": 0, "onramp_allow_usdce": onramp,
        "exch_allow": {ex: exch for ex in snd.APPROVE_TARGETS},
        "ctf_operator": {ex: ctf for ex in (pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2)},
    }


def _steps(state):
    return [s["step"] for s in snd.plan(state, TGT)]


def test_from_native_only_needs_full_funnel():
    steps = _steps(_state(native=pv.to_units(50)))
    assert steps[0] == "swap"
    assert "wrap" in steps
    assert "approve-pusd" in steps
    assert "approve-ctf" in steps
    assert "ready" not in steps


def test_with_usdce_no_swap_needed():
    steps = _steps(_state(usdce=pv.to_units(5)))
    assert "swap" not in steps          # already have USDC.e
    assert "wrap" in steps


def test_enough_pusd_skips_swap_and_wrap():
    steps = _steps(_state(pusd=pv.to_units(5)))
    assert "swap" not in steps
    assert "wrap" not in steps
    # approvals still pending
    assert "approve-pusd" in steps


def test_fully_provisioned_is_ready():
    s = _state(pusd=pv.to_units(5), onramp=pv.MAX_UINT if hasattr(pv, "MAX_UINT") else 2**256-1,
               exch=2**256 - 1, ctf=True)
    steps = _steps(s)
    assert steps == ["ready"]


def test_partial_pusd_wraps_only_the_shortfall():
    # have $2 pUSD, need $5 -> wrap detail should reference the $3 shortfall
    s = _state(usdce=pv.to_units(10), pusd=pv.to_units(2))
    wrap = [x for x in snd.plan(s, TGT) if x["step"] == "wrap"][0]
    assert "3.00" in wrap["detail"]


def test_approvals_skipped_when_already_set():
    s = _state(pusd=pv.to_units(5), exch=2**256 - 1, ctf=True)
    assert _steps(s) == ["ready"]
