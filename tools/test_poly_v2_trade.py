"""Tests for the V2 contract-config override. No mocks.

The override is the riskiest line in the trade path: if it returns the wrong
exchange or collateral, orders sign against the wrong verifyingContract and the
CLOB silently rejects them. These tests assert the patch lands in every module
that imported get_contract_config, and cross-check it against the live chain.
"""
import pytest
import polymarket_v2 as pv

pcc = pytest.importorskip("py_clob_client")
import poly_v2_trade as t


def _online() -> bool:
    try:
        pv.matic_balance("0x0000000000000000000000000000000000000000")
        return True
    except pv.ChainError:
        return False


live = pytest.mark.skipif(not _online(), reason="Polygon RPC unreachable")


def test_patch_returns_v2_addresses_and_pusd():
    fn = t.patch_v2()
    normal = fn(137, neg_risk=False)
    negrisk = fn(137, neg_risk=True)
    assert normal.exchange.lower() == pv.EXCHANGE_V2
    assert negrisk.exchange.lower() == pv.NEGRISK_EXCHANGE_V2
    assert normal.collateral.lower() == pv.PUSD          # pUSD, NOT the stale USDC.e
    assert negrisk.collateral.lower() == pv.PUSD
    assert normal.conditional_tokens.lower() == pv.CTF


def test_patch_lands_in_all_three_namespaces():
    t.patch_v2()
    import py_clob_client.config as cfg
    import py_clob_client.client as cl
    import py_clob_client.order_builder.builder as bd
    # all three must now resolve pUSD collateral (the builder uses its own ref for signing)
    for mod in (cfg, cl, bd):
        assert mod.get_contract_config(137).collateral.lower() == pv.PUSD, mod.__name__


def test_patch_rejects_unpatched_chain():
    fn = t.patch_v2()
    with pytest.raises(Exception):
        fn(1)  # mainnet not patched — must not silently return Polygon config


@live
def test_override_matches_onchain_collateral():
    """Ground-truth cross-check: the collateral our patch injects must equal what
    the live V2 exchange actually settles in (getCollateral)."""
    try:
        on_chain = pv.get_collateral(pv.EXCHANGE_V2)
    except pv.ChainError:
        pytest.skip("RPC unreachable")
    fn = t.patch_v2()
    assert on_chain is not None
    assert fn(137).collateral.lower() == on_chain.lower() == pv.PUSD
