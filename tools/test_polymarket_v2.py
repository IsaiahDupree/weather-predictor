"""
Real integration + unit tests for the Polymarket V2/pUSD self-custody layer.

NO MOCKS (per project rule). Two tiers:
  * unit  — pure functions, fully offline, always run.
  * live  — hit Polygon RPC read-only / simulate txs; assert the on-chain facts
            we reverse-engineered. Skipped (not failed) if the network is down,
            so the suite is still runnable offline.

Run:  python3 -m pytest tools/test_polymarket_v2.py -v
Only offline:  python3 -m pytest tools/test_polymarket_v2.py -v -m "not live"
"""
import pytest
import polymarket_v2 as p

FUNDER = "0xEE2D0C138E3CBf7965f6F9e6D63e6EC81007B482"   # the MetaMask 7702 acct holding the test funds


def _online() -> bool:
    try:
        p.matic_balance("0x0000000000000000000000000000000000000000")
        return True
    except p.ChainError:
        return False


live = pytest.mark.skipif(not _online(), reason="Polygon RPC unreachable — skipping live checks")


# ======================= UNIT (offline, pure) =======================

def test_selectors_match_known_4byte():
    # Canonical 4-byte selectors — if these drift, calldata is silently wrong.
    assert p.selector("balanceOf(address)") == "0x70a08231"
    assert p.selector("approve(address,uint256)") == "0x095ea7b3"
    assert p.selector("allowance(address,address)") == "0xdd62ed3e"
    assert p.selector("setApprovalForAll(address,bool)") == "0xa22cb465"
    assert p.selector("getCollateral()") == "0x5c1548fb"
    assert p.selector("wrap(address,address,uint256)") == "0x62355638"  # verified vs live onramp


def test_to_units_is_exact_money_math():
    assert p.to_units(5) == 5_000_000
    assert p.to_units(0.1) == 100_000
    assert p.to_units(2.07) == 2_070_000           # the float-trap value: must NOT be 2069999
    assert p.to_units(50.74) == 50_740_000
    assert p.from_units(50_740_000) == 50.74


def test_to_units_floors_and_rejects_negative():
    assert p.to_units(1.2345678) == 1_234_567       # floored, never rounds up (no over-spend)
    with pytest.raises(ValueError):
        p.to_units(-1)


def test_min_out_slippage():
    assert p.min_out(5_000_000, 0) == 5_000_000
    assert p.min_out(5_000_000, 100) == 4_950_000   # 1%
    assert p.min_out(1_000_000, 50) == 995_000      # 0.5%
    with pytest.raises(ValueError):
        p.min_out(1, 10_001)


def test_calldata_encoding_shapes():
    cd = p.cd_approve(p.EXCHANGE_V2, 5_000_000)
    assert cd.startswith("0x095ea7b3")
    assert len(cd) == 2 + 8 + 64 + 64               # 0x + sel + addr + uint
    assert p.EXCHANGE_V2[2:].lower() in cd.lower()

    w = p.cd_wrap(p.USDCE, FUNDER, 5_000_000)
    assert w.startswith("0x62355638")
    assert len(w) == 2 + 8 + 64 * 3                 # 3 args
    assert p.USDCE[2:].lower() in w.lower()
    assert FUNDER[2:].lower() in w.lower()


def test_approve_amount_is_encoded_not_truncated():
    big = 2**256 - 1
    cd = p.cd_approve(p.EXCHANGE_V2, big)
    assert cd.endswith("f" * 64)                    # max-uint approval encodes fully


# ======================= LIVE (real chain) =======================

@live
def test_pusd_is_the_collateral_token():
    # name()/symbol()/decimals() of pUSD
    sym = p.eth_call(p.PUSD, p.selector("symbol()"))
    dec = p.eth_call(p.PUSD, p.cd_decimals())
    # symbol is an ABI-encoded string; just assert "pUSD" bytes appear
    assert sym is not None and b"pUSD".hex() in sym.lower()
    assert int(dec, 16) == 6


@live
def test_v2_exchanges_settle_in_pusd():
    # THE migration fact: exchanges' collateral == pUSD (not USDC.e).
    for ex in (p.EXCHANGE_V2, p.NEGRISK_EXCHANGE_V2):
        col = p.get_collateral(ex)
        assert col is not None
        assert col.lower() == p.PUSD, f"{ex} collateral={col}, expected pUSD"


@live
def test_onramp_accepts_usdce_and_rejects_native_usdc():
    # THE wrap fact: CollateralOnramp.wrap accepts USDC.e, reverts on native USDC.
    ok_usdce, _ = p.simulate(p.ONRAMP, p.cd_wrap(p.USDCE, FUNDER, 0), frm=FUNDER)
    ok_native, _ = p.simulate(p.ONRAMP, p.cd_wrap(p.NATIVE_USDC, FUNDER, 0), frm=FUNDER)
    assert ok_usdce is True, "onramp should accept USDC.e"
    assert ok_native is False, "onramp must reject native USDC (this is why funds read $0)"


@live
def test_funder_is_7702_smart_account():
    # 0xEE2D is an EIP-7702 delegated EOA -> ecrecover still maps to it -> sig_type 0 valid.
    code = p.get_code(FUNDER)
    assert code.lower().startswith("0xef0100"), f"expected 7702 delegation, got {code[:12]}"


@live
def test_funder_balances_are_readable():
    # Read-only sanity: balances decode without error (values may change over time).
    nat = p.balance_of(p.NATIVE_USDC, FUNDER)
    pusd = p.balance_of(p.PUSD, FUNDER)
    assert nat >= 0 and pusd >= 0
    print(f"\n  0xEE2D native USDC=${p.from_units(nat):.2f}  pUSD=${p.from_units(pusd):.2f}")


@live
def test_swap_route_native_to_usdce_OPEN_ITEM():
    # Tracks the unresolved swap route. Direct Uniswap v3 pools showed 0 active
    # liquidity; until a viable native->USDC.e route is confirmed (aggregator),
    # this is an explicit xfail so it can't silently pass and hide the blocker.
    FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
    sel = p.selector("getPool(address,address,uint24)")
    found = []
    for fee in (100, 500, 3000):
        data = sel + p.NATIVE_USDC.replace("0x", "").rjust(64, "0") + \
               p.USDCE.replace("0x", "").rjust(64, "0") + format(fee, "x").rjust(64, "0")
        pool = p.eth_call(FACTORY, data)
        if pool and int(pool, 16) != 0:
            liq = p.eth_call("0x" + pool[-40:], p.selector("liquidity()"))
            if liq and int(liq, 16) > 0:
                found.append((fee, int(liq, 16)))
    if not found:
        pytest.xfail("native->USDC.e direct pools have 0 liquidity — route via aggregator TBD")
    assert found
