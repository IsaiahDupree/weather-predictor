"""
Intra-market arbitrage (Dutch book) scanner.

In a set of mutually-exclusive, collectively-exhaustive outcomes (the buckets of
one temperature market, or just YES+NO of one bucket), the prices *should* sum
to ~1. When the executable ask prices sum to **less than 1 minus fees**, you can
buy one share of every outcome and collect a guaranteed $1 at resolution,
locking risk-free profit:

    profit_per_set = 1 - sum(asks) - total_fees

This edge is real but narrow and fleeting — it only counts when the prices are
**executable** (enough depth to actually fill each leg) and **fee-inclusive**,
and all legs must fill near-simultaneously or the filled legs are unwound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.edge.ev import executable_cost


@dataclass
class ArbLeg:
    label: str
    avg_price: float
    qty: float


@dataclass
class ArbOpportunity:
    legs: List[ArbLeg]
    total_cost: float                 # cost to buy one full set (sum of avg prices * 1)
    fees: float
    profit_per_set: float             # 1 - total_cost - fees
    max_sets: float                   # how many full sets the thinnest leg supports
    detail: Dict[str, float] = field(default_factory=dict)

    @property
    def is_profitable(self) -> bool:
        return self.profit_per_set > 0 and self.max_sets > 0


def binary_arb(yes_ask: float, no_ask: float, fee: float = 0.0) -> Optional[float]:
    """Profit per YES+NO pair if buying both locks $1; None if no edge.

    profit = 1 - (yes_ask + no_ask) - fee.
    """
    profit = 1.0 - (yes_ask + no_ask) - fee
    return profit if profit > 0 else None


def scan(
    bucket_books: Dict[str, Sequence[Tuple[float, float]]],
    set_qty: float = 1.0,
    fee_per_contract: float = 0.0,
) -> Optional[ArbOpportunity]:
    """Detect a Dutch book across a market's buckets.

    bucket_books : {bucket_label: ascending [(ask_price, size), ...]} — the ask
                   side of each mutually-exclusive bucket.
    set_qty      : how many full sets we'd want to buy (sizing is then capped by
                   the thinnest leg's executable depth).

    Returns an ArbOpportunity when the fee-inclusive executable cost of one set
    is below $1, else None.
    """
    if not bucket_books:
        return None

    legs: List[ArbLeg] = []
    total_cost = 0.0
    min_depth = float("inf")
    for label, book in bucket_books.items():
        avg, filled, cost = executable_cost(book, set_qty)
        if filled < set_qty - 1e-9:
            # Not enough depth on this leg to complete even one requested set at
            # any price — record the depth we *can* fill so max_sets reflects it.
            depth = filled
        else:
            depth = sum(size for _, size in book)
        min_depth = min(min_depth, max(depth, 0.0))
        legs.append(ArbLeg(label=label, avg_price=avg if filled > 0 else 0.0, qty=set_qty))
        total_cost += (avg if filled > 0 else 1.0) * set_qty

    n_legs = len(legs)
    fees = fee_per_contract * n_legs * set_qty
    payout = 1.0 * set_qty
    profit = payout - total_cost - fees
    max_sets = 0.0 if min_depth == float("inf") else min_depth
    opp = ArbOpportunity(
        legs=legs,
        total_cost=total_cost,
        fees=fees,
        profit_per_set=profit / set_qty if set_qty else 0.0,
        max_sets=max_sets,
        detail={"sum_asks": total_cost / set_qty if set_qty else 0.0, "n_legs": float(n_legs)},
    )
    return opp if opp.is_profitable else None
