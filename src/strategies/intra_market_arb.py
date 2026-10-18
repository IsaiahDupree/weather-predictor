"""
Intra-market arbitrage (Dutch book).

The buckets of one temperature event are mutually exclusive and collectively
exhaustive, so their YES prices should sum to ~1. When the *executable*,
fee-inclusive YES asks sum below 1, buy one share of every bucket and collect a
guaranteed $1 at resolution — risk-free profit = 1 - sum(asks) - fees.

This is the safest edge (no forecast/obs dependency), but narrow and fleeting:
it only counts on executable depth, and all legs must fill near-simultaneously
or the filled legs are immediately unwound (the loop/adapter handles the unwind;
this strategy only flags the opportunity and sizes it to the thinnest leg).
"""

from __future__ import annotations

from typing import List

from src.edge.arb_scanner import scan
from src.strategies.base_strategy import Signal, Strategy
from src.strategies.common import signal_meta
from src.market.snapshot import MarketSnapshot


class IntraMarketArb(Strategy):
    name = "intra_market_arb"
    kelly_lambda = 1.0            # risk-free; sized to thinnest-leg depth, not Kelly

    set_qty: float = 1.0

    def generate_signals(self, snapshot: MarketSnapshot) -> List[Signal]:
        # Build the ask-side book for every bucket's YES market.
        bucket_books = {}
        for quote in snapshot.quotes:
            levels = quote.yes_ask_levels or ([(quote.yes_ask, quote.yes_ask_size or 1.0)]
                                              if quote.yes_ask > 0 else [])
            if levels:
                bucket_books[quote.label] = levels
        # Need the full set to guarantee $1 — a partial set isn't a Dutch book.
        if len(bucket_books) < len(snapshot.quotes) or not bucket_books:
            return []

        opp = scan(bucket_books, set_qty=self.set_qty,
                   fee_per_contract=snapshot.fee_per_contract)
        if opp is None or not opp.is_profitable:
            return []

        out: List[Signal] = []
        for leg in opp.legs:
            quote = snapshot.quote_for(leg.label)
            if quote is None:
                continue
            meta = signal_meta(snapshot, quote, q=1.0, p=leg.avg_price, outcome="yes",
                               strategy=self.name, is_arbitrage=True,
                               book_depth=opp.max_sets)
            meta["arb_profit_per_set"] = opp.profit_per_set
            meta["arb_sum_asks"] = opp.detail.get("sum_asks")
            out.append(Signal(
                side="BUY", outcome="yes", target_price=leg.avg_price,
                strength=1.0,
                reason=f"DUTCH BOOK leg {leg.label} @ {leg.avg_price:.3f} "
                       f"(set cost {opp.total_cost:.3f}, profit/set {opp.profit_per_set:.3f})",
                metadata=meta,
            ))
        return out
