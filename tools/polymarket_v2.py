"""
polymarket_v2.py — V2/pUSD self-custody primitives for Polymarket on Polygon.

Pure, testable helpers (calldata encoding, unit + slippage math) plus a thin
read-only chain client (balanceOf / allowance / getCollateral / tx simulation).
No private keys, no broadcasting here — this module only READS and SIMULATES.
The write scripts import these helpers; tests assert them against live chain.

Everything reverse-engineered + verified 2026-06-14. See the project memory
`polymarket-v2-pusd-selfcustody` for the why.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from decimal import Decimal, ROUND_DOWN
from eth_utils import keccak

CHAIN_ID = 137

# --- Canonical addresses (lowercase) -------------------------------------
NATIVE_USDC = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"   # Circle native USDC
USDCE       = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"   # bridged USDC.e
PUSD        = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"   # Polymarket USD (collateral)
ONRAMP      = "0x93070a847efef7f70739046a929d47a521f5b8ee"   # CollateralOnramp (USDC.e -> pUSD)
CTF         = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"   # ConditionalTokens
EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"   # CTF Exchange V2
NEGRISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310f59"
NEGRISK_ADAPTER     = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"
# pUSD must be approved to these spenders before the CLOB sees collateral:
EXCHANGE_SPENDERS = [EXCHANGE_V2, NEGRISK_EXCHANGE_V2, NEGRISK_ADAPTER]

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0"}

# --- Pure helpers (no network) -------------------------------------------

def selector(sig: str) -> str:
    """4-byte function selector, hex with 0x."""
    return "0x" + keccak(text=sig)[:4].hex()


def _addr32(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _uint32(n: int) -> str:
    if n < 0:
        raise ValueError("uint cannot be negative")
    return format(n, "x").rjust(64, "0")


def to_units(usd, decimals: int = 6) -> int:
    """Dollars -> integer token units. Exact (Decimal), floors to avoid over-spend."""
    q = (Decimal(str(usd)) * (10 ** decimals)).to_integral_value(rounding=ROUND_DOWN)
    if q < 0:
        raise ValueError("amount cannot be negative")
    return int(q)


def from_units(raw: int, decimals: int = 6) -> float:
    return raw / (10 ** decimals)


def min_out(amount_in: int, slippage_bps: int) -> int:
    """Minimum acceptable output given slippage in basis points (1% = 100)."""
    if not 0 <= slippage_bps <= 10_000:
        raise ValueError("slippage_bps out of range")
    return (amount_in * (10_000 - slippage_bps)) // 10_000


# ERC-20 calldata
def cd_balance_of(owner: str) -> str:
    return selector("balanceOf(address)") + _addr32(owner)


def cd_allowance(owner: str, spender: str) -> str:
    return selector("allowance(address,address)") + _addr32(owner) + _addr32(spender)


def cd_approve(spender: str, amount: int) -> str:
    return selector("approve(address,uint256)") + _addr32(spender) + _uint32(amount)


def cd_decimals() -> str:
    return selector("decimals()")


# CTF (ERC-1155) operator approval — needed to SELL outcome tokens
def cd_set_approval_for_all(operator: str, approved: bool) -> str:
    return selector("setApprovalForAll(address,bool)") + _addr32(operator) + _uint32(1 if approved else 0)


def cd_is_approved_for_all(owner: str, operator: str) -> str:
    return selector("isApprovedForAll(address,address)") + _addr32(owner) + _addr32(operator)


# CollateralOnramp wrap: wrap(address _asset, address _to, uint256 _amount)
def cd_wrap(asset: str, to: str, amount: int) -> str:
    return selector("wrap(address,address,uint256)") + _addr32(asset) + _addr32(to) + _uint32(amount)


# Exchange getCollateral() view
def cd_get_collateral() -> str:
    return selector("getCollateral()")


# --- Read-only chain client ----------------------------------------------

class ChainError(RuntimeError):
    pass


def _rpc(method: str, params: list):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    errs = []
    for url in RPCS:
        try:
            req = urllib.request.Request(url, data=payload, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                out = json.loads(r.read())
            return out  # includes either 'result' or 'error'
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            errs.append(f"{url}: {type(e).__name__}")
            continue
    raise ChainError("all RPCs unreachable: " + "; ".join(errs))


def eth_call(to: str, data: str, frm: str | None = None) -> str | None:
    """Returns hex result, or None if the call reverted."""
    if not data.startswith("0x"):
        data = "0x" + data
    p = {"to": to, "data": data}
    if frm:
        p["from"] = frm
    out = _rpc("eth_call", [p, "latest"])
    if "error" in out:
        return None
    res = out.get("result")
    return res if res not in (None, "0x") else None


def simulate(to: str, data: str, frm: str | None = None) -> tuple[bool, str]:
    """Static-simulate a state-changing call. (ok, detail). ok=False on revert."""
    if not data.startswith("0x"):
        data = "0x" + data
    p = {"to": to, "data": data}
    if frm:
        p["from"] = frm
    out = _rpc("eth_call", [p, "latest"])
    if "error" in out:
        return False, str(out["error"].get("message", "reverted"))
    return True, "ok"


def get_code(addr: str) -> str:
    out = _rpc("eth_getCode", [addr, "latest"])
    return out.get("result", "") or ""


def estimate_gas(to: str, data: str, frm: str) -> int | None:
    """eth_estimateGas for a call; None if it would revert. Used to right-size gas
    limits (hardcoding too low silently fails the tx)."""
    if not data.startswith("0x"):
        data = "0x" + data
    out = _rpc("eth_estimateGas", [{"from": frm, "to": to, "data": data}])
    if "error" in out:
        return None
    return int(out["result"], 16)


def balance_of(token: str, owner: str) -> int:
    r = eth_call(token, cd_balance_of(owner))
    return int(r, 16) if r else 0


def allowance(token: str, owner: str, spender: str) -> int:
    r = eth_call(token, cd_allowance(owner, spender))
    return int(r, 16) if r else 0


def get_collateral(exchange: str) -> str | None:
    r = eth_call(exchange, cd_get_collateral())
    return ("0x" + r[-40:]) if r else None


def matic_balance(addr: str) -> int:
    out = _rpc("eth_getBalance", [addr, "latest"])
    return int(out.get("result", "0x0"), 16)
