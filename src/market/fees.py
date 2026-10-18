"""
Venue fee models.

Kalshi charges a parabolic trading fee that peaks at price = 0.50 and must be
netted into EV — it can erase the edge on cheap high-frequency scalping. The
published taker formula is fee = roundup(0.07 * C * P * (1 - P)) (per the
2026-02-05 schedule), where C = contract count and P = price in [0,1]; resting
maker orders are often fee-exempt. Polymarket charges ~0 trading fees (gas
aside, which is gasless via the relayer).
"""

from __future__ import annotations

import math

KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175


def kalshi_fee(price: float, count: float, rate: float = KALSHI_TAKER_RATE) -> float:
    """Kalshi fee in DOLLARS for ``count`` contracts at ``price`` (round up to cent)."""
    raw = rate * count * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def kalshi_fee_per_contract(price: float, rate: float = KALSHI_TAKER_RATE) -> float:
    """Per-contract Kalshi fee at ``price`` (not rounded — for EV thresholds)."""
    return rate * price * (1.0 - price)


def fee_per_contract(venue: str, price: float, *, maker: bool = False) -> float:
    """Estimated per-contract fee for EV netting, by venue."""
    if venue == "kalshi":
        rate = KALSHI_MAKER_RATE if maker else KALSHI_TAKER_RATE
        return kalshi_fee_per_contract(price, rate)
    return 0.0  # polymarket: ~0 trading fee


def worst_case_fee_per_contract(venue: str) -> float:
    """Conservative per-contract fee (peaks at p=0.5) for arb/threshold guards."""
    if venue == "kalshi":
        return kalshi_fee_per_contract(0.5)   # 0.07 * 0.25 = 0.0175
    return 0.0
