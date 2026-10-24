#!/usr/bin/env python3
"""
poly_v2_trade.py — V2-aware buy/sell on Polymarket from a self-custody wallet.

The pip `py_clob_client` is STALE: its get_contract_config hardcodes the OLD
exchange (0x4bFb…) and USDC.e collateral. We monkeypatch it to the V2 exchanges
+ pUSD collateral (both verified on-chain), in every module that imported it, so
order signing targets the right verifyingContract and get_balance_allowance reads
pUSD — the balance that's been $0 in every prior attempt.

HONEST CAVEAT: the V2 EIP-712 order struct/domain match is UNVERIFIED with this
old client. `check` proves the CLOB sees your pUSD collateral (the key milestone).
`buy`/`sell` build + sign the order and, only with --execute, POST it — then print
the CLOB's exact response. If the CLOB rejects the signature, we know the struct
differs and we upgrade the client / port the V2 order-utils. Nothing is hidden.

  python3 poly_v2_trade.py check                     # prove CLOB sees pUSD collateral
  python3 poly_v2_trade.py book   --token <id>       # order book
  python3 poly_v2_trade.py buy    --token <id> --price 0.50 --size 2          # dry-run
  python3 poly_v2_trade.py buy    --token <id> --price 0.50 --size 2 --execute  # POST
  python3 poly_v2_trade.py sell   --token <id> --price 0.55 --size 2 --execute
"""
import argparse
import sys

import polymarket_v2 as pv
from poly_v2_send import load_env

HOST = "https://clob.polymarket.com"

# V2 contract config (checksummed where it matters; py_clob_client lower-cases internally)
_V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
_V2_NEGRISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def patch_v2():
    """Override get_contract_config -> V2 addresses + pUSD, in ALL modules that
    imported it (config source + client + builder). Returns the patched fn."""
    import py_clob_client.config as cfg
    from py_clob_client.clob_types import ContractConfig

    def v2_get_contract_config(chainID: int, neg_risk: bool = False):
        if chainID != pv.CHAIN_ID:
            raise Exception(f"only chain {pv.CHAIN_ID} patched, got {chainID}")
        ex = _V2_NEGRISK_EXCHANGE if neg_risk else _V2_EXCHANGE
        return ContractConfig(exchange=ex, collateral=_PUSD, conditional_tokens=_CTF)

    cfg.get_contract_config = v2_get_contract_config
    # client.py and builder.py did `from ..config import get_contract_config`
    import py_clob_client.client as cl
    import py_clob_client.order_builder.builder as bd
    cl.get_contract_config = v2_get_contract_config
    bd.get_contract_config = v2_get_contract_config
    return v2_get_contract_config


def build_client(env, level2=True):
    patch_v2()
    from py_clob_client.client import ClobClient
    pk = env.get("POLYMARKET_PK")
    funder = env["POLYMARKET_FUNDER"]
    if not pk:
        print("POLYMARKET_PK missing in tools/.env (needed for trading)"); sys.exit(1)
    c = ClobClient(HOST, key=pk, chain_id=pv.CHAIN_ID, signature_type=0, funder=funder)
    if level2:
        c.set_api_creds(c.create_or_derive_api_creds())
    return c


# ----------------------------- commands -----------------------------

def cmd_check(env, args):
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    c = build_client(env)
    # prove the override took: the collateral the client now uses == pUSD
    from py_clob_client.config import get_contract_config
    col = get_contract_config(pv.CHAIN_ID).collateral
    print(f"client collateral override: {col}  ({'pUSD ✓' if col.lower()==_PUSD.lower() else 'WRONG ✗'})")
    ba = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    bal = int(ba.get("balance", "0")) / 1e6
    print(f"CLOB collateral balance: ${bal:.2f}")
    print(f"allowances: {ba.get('allowances', {})}")
    if bal > 0:
        print("✅ Polymarket SEES your pUSD collateral — ready to place orders.")
    else:
        print("⚠️  still $0 — wrap pUSD + set approvals first (poly_v2_send).")


def cmd_book(env, args):
    c = build_client(env, level2=False)
    ob = c.get_order_book(args.token)
    bids = ob.bids[:3] if hasattr(ob, "bids") else []
    asks = ob.asks[:3] if hasattr(ob, "asks") else []
    print(f"token {args.token}")
    print("  asks:", [(a.price, a.size) for a in asks])
    print("  bids:", [(b.price, b.size) for b in bids])


def _place(env, args, side):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    c = build_client(env)
    oa = OrderArgs(token_id=args.token, price=args.price, size=args.size,
                   side=(BUY if side == "buy" else SELL))
    signed = c.create_order(oa)   # L1 sign (no network)
    notional = args.price * args.size
    print(f"{side.upper()} {args.size} @ ${args.price}  (notional ${notional:.2f})  token {args.token}")
    print(f"  signed order built (maker={env['POLYMARKET_FUNDER']}, sig_type=0)")
    if not args.execute:
        print("  DRY-RUN: not posted. Re-run with --execute to POST to the CLOB.")
        return
    try:
        resp = c.post_order(signed, OrderType.GTC)
        print("  CLOB response:", resp)
    except Exception as e:
        msg = str(e)
        if "invalid order version" in msg:
            print("  ✗ CLOB rejected: V1 order struct. This published py-clob-client cannot build")
            print("    V2 orders — placement needs the V2 (TypeScript) client. Funds/collateral are fine.")
        else:
            print(f"  ✗ CLOB rejected: {msg[:200]}")


def cmd_buy(env, args):  _place(env, args, "buy")
def cmd_sell(env, args): _place(env, args, "sell")


def cmd_cancel(env, args):
    c = build_client(env)
    resp = c.cancel_all()
    print("cancel_all response:", resp)


def main():
    ap = argparse.ArgumentParser(description="Polymarket V2 self-custody trade")
    ap.add_argument("cmd", choices=["check", "book", "buy", "sell", "cancel"])
    ap.add_argument("--token", help="ERC-1155 outcome token id")
    ap.add_argument("--price", type=float, help="limit price in [0,1]")
    ap.add_argument("--size", type=float, help="number of shares")
    ap.add_argument("--execute", action="store_true", help="POST the order (default: dry-run)")
    args = ap.parse_args()
    env = load_env()
    if not env.get("POLYMARKET_FUNDER"):
        print("set POLYMARKET_FUNDER in tools/.env"); sys.exit(1)
    if args.cmd in ("book", "buy", "sell") and not args.token:
        print(f"{args.cmd} needs --token"); sys.exit(1)
    if args.cmd in ("buy", "sell") and (args.price is None or args.size is None):
        print("buy/sell need --price and --size"); sys.exit(1)
    if args.execute:
        print("⚠️  --execute: will POST a REAL order to the CLOB.\n")
    {"check": cmd_check, "book": cmd_book, "buy": cmd_buy, "sell": cmd_sell, "cancel": cmd_cancel}[args.cmd](env, args)


if __name__ == "__main__":
    main()
