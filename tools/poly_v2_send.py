#!/usr/bin/env python3
"""
poly_v2_send.py — walk self-custody funds into Polymarket-tradeable pUSD.

The funnel (each arrow = on-chain tx, needs a little POL for gas):

  native USDC ──swap──> USDC.e ──wrap──> pUSD ──approve──> [CLOB sees collateral]

Subcommands:
  status                 read-only funnel view + what's pending (no key needed)
  swap   --usd 5         native USDC -> USDC.e  (Uniswap v3, best fee tier, slippage-guarded)
  wrap   --usd 5         USDC.e -> pUSD         (CollateralOnramp)
  approve                pUSD -> V2 exchanges + CTF operator approval (for selling)

SAFETY: every command DRY-RUNS by default — it builds the tx, static-simulates it
(eth_call), and prints what it WOULD do. Nothing broadcasts until you add --execute.
The key is read from tools/.env (POLYMARKET_PK) and never logged.

  python3 poly_v2_send.py status
  python3 poly_v2_send.py swap --usd 5            # dry run (shows live quote)
  python3 poly_v2_send.py swap --usd 5 --execute  # broadcasts
"""
import argparse
import os
import sys
import time

import polymarket_v2 as pv

# --- Uniswap v3 (Polygon) for the native-USDC -> USDC.e leg ---
UNISWAP_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
UNISWAP_QUOTER  = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"   # QuoterV2
UNISWAP_ROUTER  = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"   # SwapRouter02 (no deadline arg)
FEE_TIERS = (100, 500, 3000)
MAX_UINT = 2**256 - 1

# Spenders that need pUSD approval + CTF operator approval to trade/sell:
APPROVE_TARGETS = [pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2, pv.NEGRISK_ADAPTER]


# ----------------------------- env / key -----------------------------

def load_env():
    """Minimal .env loader (no dependency). Looks next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, ".env")
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # process env overrides file
    for k in ("POLYMARKET_FUNDER", "POLYMARKET_PK", "POLYGON_RPC"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# ----------------------------- pure planning -----------------------------

def plan(state: dict, target_units: int) -> list[dict]:
    """Given the on-chain funnel state, return the ordered list of pending steps
    to make `target_units` of pUSD tradeable. PURE — fully unit-testable.

    state keys (all int unit amounts / bools):
      native, usdce, pusd, matic,
      router_allow_native, onramp_allow_usdce,
      exch_allow:{addr:int}, ctf_operator:{addr:bool}
    """
    steps = []
    have_pusd = state["pusd"]
    if have_pusd < target_units:
        need = target_units - have_pusd
        # need `need` more pUSD -> must wrap that much USDC.e -> may need to swap first
        if state["usdce"] < need:
            short = need - state["usdce"]
            steps.append({"step": "swap", "detail": f"native USDC -> USDC.e for {pv.from_units(short):.2f}"})
        steps.append({"step": "wrap", "detail": f"USDC.e -> pUSD for {pv.from_units(need):.2f}"})
    # approvals (independent of amount; needed once)
    for ex in APPROVE_TARGETS:
        if state["exch_allow"].get(ex, 0) < target_units:
            steps.append({"step": "approve-pusd", "spender": ex, "detail": f"approve pUSD -> {ex[:10]}…"})
    for ex in (pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2):
        if not state["ctf_operator"].get(ex, False):
            steps.append({"step": "approve-ctf", "operator": ex, "detail": f"CTF setApprovalForAll -> {ex[:10]}… (to sell)"})
    if not steps:
        steps.append({"step": "ready", "detail": "all set — collateral is tradeable"})
    return steps


# ----------------------------- chain reads -----------------------------

def read_state(funder: str) -> dict:
    return {
        "native": pv.balance_of(pv.NATIVE_USDC, funder),
        "usdce":  pv.balance_of(pv.USDCE, funder),
        "pusd":   pv.balance_of(pv.PUSD, funder),
        "matic":  pv.matic_balance(funder),
        "router_allow_native": pv.allowance(pv.NATIVE_USDC, funder, UNISWAP_ROUTER),
        "onramp_allow_usdce":  pv.allowance(pv.USDCE, funder, pv.ONRAMP),
        "exch_allow": {ex: pv.allowance(pv.PUSD, funder, ex) for ex in APPROVE_TARGETS},
        "ctf_operator": {ex: _is_operator(funder, ex) for ex in (pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2)},
    }


def _is_operator(owner, operator) -> bool:
    r = pv.eth_call(pv.CTF, pv.cd_is_approved_for_all(owner, operator))
    return bool(r) and int(r, 16) == 1


def best_swap_quote(amount_in: int):
    """Resolve the native->USDC.e route: best (fee, amount_out) via QuoterV2, or None."""
    sel = pv.selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
    best = None
    for fee in FEE_TIERS:
        data = (sel + pv.NATIVE_USDC.replace("0x", "").rjust(64, "0")
                + pv.USDCE.replace("0x", "").rjust(64, "0")
                + format(amount_in, "x").rjust(64, "0")
                + format(fee, "x").rjust(64, "0") + "0" * 64)
        r = pv.eth_call(UNISWAP_QUOTER, data)
        if r:
            out = int(r[2:66], 16)
            if best is None or out > best[1]:
                best = (fee, out)
    return best


# ----------------------------- tx send -----------------------------

def _w3(env):
    """Connect to the first reachable Polygon RPC (failover across endpoints)."""
    from web3 import Web3
    candidates = ([env["POLYGON_RPC"]] if env.get("POLYGON_RPC") else []) + list(pv.RPCS)
    last = None
    for rpc in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
            if w3.is_connected():
                w3.eth.chain_id  # force a live round-trip
                return w3
        except Exception as e:
            last = e
            continue
    raise ConnectionError(f"no reachable Polygon RPC ({last})")


def gas_with_buffer(estimate: int, pct: int = 30) -> int:
    """Add headroom over an eth_estimateGas result. Hardcoding a flat limit is the
    bug that bit us: the USDC.e→onramp approve needs ~81k, an 80k cap silently failed."""
    if estimate <= 0:
        raise ValueError("gas estimate must be positive")
    return (estimate * (100 + pct)) // 100


def _get_w3(env):
    """One shared web3 connection per command run, so sequential txs (approve→wrap)
    read consistent state from the same node (no cross-endpoint lag)."""
    if env.get("_w3") is None:
        env["_w3"] = _w3(env)
    return env["_w3"]


def send_tx(env, to, data, value=0, gas=300_000, label=""):
    """Dry-run simulate, or (if --execute) estimate-gas → broadcast. Returns (ok, info). Never raises."""
    funder = env["POLYMARKET_FUNDER"]
    if not env.get("_execute"):
        ok, detail = pv.simulate(to, data, frm=funder)
        print(f"   simulate {label}: {'OK' if ok else 'REVERT -> ' + detail}")
        return (True, "dry-run (not broadcast)") if ok else (False, detail)
    pk = env.get("POLYMARKET_PK")
    if not pk:
        return False, "POLYMARKET_PK missing in tools/.env"
    try:
        from web3 import Web3
        w3 = _get_w3(env)
        acct = w3.eth.account.from_key(pk)
        if acct.address.lower() != funder.lower():
            return False, f"key derives to {acct.address}, not funder {funder}"
        txbase = {"from": acct.address, "to": Web3.to_checksum_address(to),
                  "data": data if data.startswith("0x") else "0x" + data, "value": value}
        # estimate gas (also validates the tx won't revert); retry to absorb state-propagation lag
        gas_est, err = None, None
        for _ in range(4):
            try:
                gas_est = w3.eth.estimate_gas(txbase); break
            except Exception as e:
                err = e; time.sleep(2)
        if gas_est is None:
            return False, f"would revert: {str(err)[:140]}"
        gas = gas_with_buffer(gas_est)                 # 30% headroom over the estimate
        maxfee = w3.eth.gas_price * 2
        need, bal = maxfee * gas, w3.eth.get_balance(acct.address)
        if bal < need:
            return False, f"out of gas: need ~{need/1e18:.3f} POL, have {bal/1e18:.4f}. Send POL to {acct.address}"
        tx = {**txbase, "chainId": pv.CHAIN_ID, "nonce": w3.eth.get_transaction_count(acct.address),
              "gas": gas, "maxFeePerGas": maxfee, "maxPriorityFeePerGas": w3.to_wei(30, "gwei")}
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        hh = h.hex(); hh = hh if hh.startswith("0x") else "0x" + hh
        print(f"   broadcast {label} (gas {gas}): https://polygonscan.com/tx/{hh}")
        rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        return (rc.status == 1), ("confirmed ✓" if rc.status == 1 else "tx reverted on-chain")
    except Exception as e:
        msg = str(e)
        if "insufficient funds" in msg:
            return False, f"out of gas — send ~0.2 POL to {funder}"
        return False, f"network/RPC error: {msg[:140]}"


# ----------------------------- commands -----------------------------

def cmd_status(env, args):
    f = env["POLYMARKET_FUNDER"]
    print(f"Funnel state for {f}\n")
    s = read_state(f)
    print(f"  native USDC : ${pv.from_units(s['native']):>8.2f}")
    print(f"  USDC.e      : ${pv.from_units(s['usdce']):>8.2f}")
    print(f"  pUSD        : ${pv.from_units(s['pusd']):>8.2f}   <- what the CLOB reads")
    print(f"  POL (gas)   : {s['matic']/1e18:>9.4f}")
    print("\n  approvals:")
    print(f"    USDC.e -> onramp : {'set' if s['onramp_allow_usdce']>0 else 'none'}")
    for ex in APPROVE_TARGETS:
        print(f"    pUSD -> {ex[:12]}… : {'set' if s['exch_allow'][ex]>0 else 'none'}")
    for ex in (pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2):
        print(f"    CTF op {ex[:12]}…  : {'set' if s['ctf_operator'][ex] else 'none'}")
    tgt = pv.to_units(args.usd)
    print(f"\n  plan to make ${args.usd:.2f} pUSD tradeable:")
    for i, st in enumerate(plan(s, tgt), 1):
        print(f"    {i}. {st['detail']}")
    q = best_swap_quote(pv.to_units(args.usd))
    if q:
        fee, out = q
        print(f"\n  live swap quote ${args.usd:.2f}: native USDC -> {pv.from_units(out):.4f} USDC.e "
              f"@ {fee/10000:.2f}% ({(out/pv.to_units(args.usd)-1)*100:+.3f}%)")
    else:
        print(f"\n  live swap quote: NO direct route — needs aggregator (route unresolved)")


def cmd_swap(env, args):
    amount = pv.to_units(args.usd)
    s = read_state(env["POLYMARKET_FUNDER"])
    if s["native"] < amount:
        print(f"insufficient native USDC: have ${pv.from_units(s['native']):.2f}"); return
    q = best_swap_quote(amount)
    if not q:
        print("no swap route (direct pools empty) — aggregator route needed, aborting"); return
    fee, out = q
    out_min = pv.min_out(out, args.slippage_bps)
    print(f"swap ${args.usd:.2f} native USDC -> ~{pv.from_units(out):.4f} USDC.e @ {fee/10000:.2f}% "
          f"(min {pv.from_units(out_min):.4f}, slippage {args.slippage_bps}bps)")
    # 1) approve native USDC -> router
    if s["router_allow_native"] < amount:
        ok, info = send_tx(env, pv.NATIVE_USDC, pv.cd_approve(UNISWAP_ROUTER, MAX_UINT), label="approve native->router")
        print("  ->", info)
        if not ok:
            return
    # 2) exactInputSingle
    sel = pv.selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
    params = (pv.NATIVE_USDC.replace("0x", "").rjust(64, "0")
              + pv.USDCE.replace("0x", "").rjust(64, "0")
              + format(fee, "x").rjust(64, "0")
              + env["POLYMARKET_FUNDER"].replace("0x", "").lower().rjust(64, "0")
              + format(amount, "x").rjust(64, "0")
              + format(out_min, "x").rjust(64, "0") + "0" * 64)
    ok, info = send_tx(env, UNISWAP_ROUTER, sel + params, gas=300_000, label="swap")
    print("  ->", info)


def cmd_wrap(env, args):
    f = env["POLYMARKET_FUNDER"]
    amount = pv.to_units(args.usd)
    s = read_state(f)
    if s["usdce"] < amount:
        print(f"insufficient USDC.e: have ${pv.from_units(s['usdce']):.2f} (swap first)"); return
    if s["onramp_allow_usdce"] < amount:
        ok, info = send_tx(env, pv.USDCE, pv.cd_approve(pv.ONRAMP, MAX_UINT), label="approve USDC.e->onramp")
        print("  ->", info)
        if not ok:
            return
    ok, info = send_tx(env, pv.ONRAMP, pv.cd_wrap(pv.USDCE, f, amount), label="wrap->pUSD")
    print("  ->", info)


def cmd_approve(env, args):
    f = env["POLYMARKET_FUNDER"]
    s = read_state(f)
    for ex in APPROVE_TARGETS:
        if s["exch_allow"][ex] == 0:
            ok, info = send_tx(env, pv.PUSD, pv.cd_approve(ex, MAX_UINT), label=f"approve pUSD->{ex[:10]}")
            print("  ->", info)
    for ex in (pv.EXCHANGE_V2, pv.NEGRISK_EXCHANGE_V2):
        if not s["ctf_operator"][ex]:
            ok, info = send_tx(env, pv.CTF, pv.cd_set_approval_for_all(ex, True), label=f"CTF op->{ex[:10]}")
            print("  ->", info)
    print("  approvals done (dry-run unless --execute)")


def main():
    ap = argparse.ArgumentParser(description="Polymarket V2 self-custody funnel")
    ap.add_argument("cmd", choices=["status", "swap", "wrap", "approve"])
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=int, default=50)
    ap.add_argument("--execute", action="store_true", help="broadcast (default: dry-run/simulate only)")
    args = ap.parse_args()
    env = load_env()
    if not env.get("POLYMARKET_FUNDER"):
        print("set POLYMARKET_FUNDER in tools/.env (copy from .env.polymarket.example)"); sys.exit(1)
    env["_execute"] = args.execute
    if args.execute and args.cmd != "status":
        print("⚠️  --execute: will BROADCAST real transactions.\n")
    {"status": cmd_status, "swap": cmd_swap, "wrap": cmd_wrap, "approve": cmd_approve}[args.cmd](env, args)


if __name__ == "__main__":
    main()
