#!/usr/bin/env python3
"""
poly_preflight.py — read-only Polymarket on-chain ground-truth check.

Python analog of polymarket-weather's v2Preflight(): given one or more wallet
addresses (your DEPOSIT-WALLET proxy and/or signer EOA), it reads the REAL
on-chain collateral balance + exchange allowances directly from Polygon RPC.

Why this exists: for POLY_1271 / email-deposit accounts (signature_type=3) the
CLOB balance API returns $0 *by design* (collateral is validated on-chain at
fill time). So "the CLOB sees $0" is NOT proof of no funds. The only ground
truth is balanceOf(collateral, funder) at the correct deposit-wallet address.

No private keys, no order placement, no external dependencies (stdlib urllib).

Usage:
    python3 poly_preflight.py 0xYourDepositWallet [0xAnotherAddr ...]

If you don't know the deposit-wallet address: open Polymarket -> Deposit; the
address shown there (a contract, not your EOA) is the funder you want.
"""
import json
import sys
import urllib.request
import urllib.error

# --- Canonical Polymarket contracts on Polygon PoS (chain 137) ---
# These match py_clob_client's get_contract_config(137). Stable, well-known.
EXCHANGES = {
    "CTF Exchange (V2)":         "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NegRisk CTF Exchange (V2)": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "NegRisk Adapter":           "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}
# Candidate collateral tokens — check ALL so a wrong-token assumption can't
# produce a false $0. USDC.e is the canonical CLOB collateral; native USDC and
# bridged USDT are checked in case funds landed un-converted.
COLLATERAL_TOKENS = {
    "USDC.e (CLOB collateral)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC (native)":            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "USDT (bridged)":           "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
}

# Public Polygon RPCs, tried in order until one answers.
RPCS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
]

BAL_SELECTOR = "0x70a08231"        # balanceOf(address)
ALLOW_SELECTOR = "0xdd62ed3e"      # allowance(address owner, address spender)
DECIMALS_SELECTOR = "0x313ce567"   # decimals()


DEBUG = "--debug" in sys.argv

# Some public RPCs 403 the default urllib User-Agent; send a browser-like one.
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    errs = []
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=payload, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                out = json.loads(r.read())
            if "error" in out:
                errs.append(f"{url}: rpc-error {out['error']}")
                continue
            return out["result"], url
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            errs.append(f"{url}: {type(e).__name__} {e}")
            continue
    if DEBUG:
        for e in errs:
            print(f"    [rpc] {e}", file=sys.stderr)
    raise RuntimeError(f"all {len(RPCS)} Polygon RPCs failed; run with --debug for per-RPC detail")


def _pad(addr):
    return addr.lower().replace("0x", "").rjust(64, "0")


def eth_call(to, data):
    res, _ = _rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return int(res, 16) if res and res != "0x" else 0


def get_code(addr):
    res, _ = _rpc("eth_getCode", [addr, "latest"])
    return res if res and res != "0x" else ""


def balance_of(token, owner):
    return eth_call(token, BAL_SELECTOR + _pad(owner))


def allowance(token, owner, spender):
    return eth_call(token, ALLOW_SELECTOR + _pad(owner) + _pad(spender))


def decimals(token):
    try:
        return eth_call(token, DECIMALS_SELECTOR) or 6
    except Exception:
        return 6


def fmt_usd(raw, dec):
    return f"${raw / (10 ** dec):,.2f}"


def check_address(addr):
    print(f"\n{'='*64}\nADDRESS: {addr}")
    code = get_code(addr)
    kind = "CONTRACT (proxy / deposit wallet ✓)" if code else "EOA (plain wallet)"
    print(f"  type: {kind}")

    any_funds = False
    print("\n  Collateral balances (on-chain ground truth):")
    for name, token in COLLATERAL_TOKENS.items():
        try:
            dec = decimals(token)
            raw = balance_of(token, addr)
        except Exception as e:
            print(f"    {name:28s} ERROR {e}")
            continue
        flag = "  <-- FUNDS HERE" if raw > 0 else ""
        if raw > 0:
            any_funds = True
        print(f"    {name:28s} {fmt_usd(raw, dec):>14s}{flag}")

    print("\n  USDC.e allowance to exchanges (must be >0 to trade):")
    usdce = COLLATERAL_TOKENS["USDC.e (CLOB collateral)"]
    for name, ex in EXCHANGES.items():
        try:
            a = allowance(usdce, addr, ex)
        except Exception as e:
            print(f"    {name:28s} ERROR {e}")
            continue
        state = "approved ✓" if a > 0 else "NOT approved"
        amt = "unlimited" if a > (1 << 200) else fmt_usd(a, 6)
        print(f"    {name:28s} {state:14s} ({amt})")
    return any_funds


def main():
    addrs = [a for a in sys.argv[1:] if a.startswith("0x") and len(a) == 42]
    if not addrs:
        print(__doc__)
        print("ERROR: pass at least one 0x-address (the deposit-wallet proxy).")
        sys.exit(1)

    print("Polymarket on-chain preflight (Polygon 137) — READ ONLY")
    funded = False
    for a in addrs:
        if check_address(a):
            funded = True

    print(f"\n{'='*64}\nVERDICT:")
    if funded:
        print("  ✓ On-chain collateral found. If allowance shows NOT approved,")
        print("    run the gasless approve before placing orders. Configure the")
        print("    bot: signature_type=3, funder=<the address holding funds>.")
    else:
        print("  ✗ No on-chain collateral at any checked address/token.")
        print("    Most likely the deposit-wallet proxy is a DIFFERENT address")
        print("    than the ones checked. Open Polymarket -> Deposit and pass")
        print("    that exact address here. (The signer EOA will read $0 — the")
        print("    funds live in the deposit-wallet proxy, not the signer.)")


if __name__ == "__main__":
    main()
