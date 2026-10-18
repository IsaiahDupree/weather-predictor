"""
Tail-bucket sniper — the @de1lymoon Taipei trade.

Buy cheap YES on temperature buckets the market prices as near-impossible
(0.3¢–15¢) but the forecast distribution gives a meaningfully higher
probability. The payoff is wildly asymmetric: at a 0.7¢ entry a winning share
returns ~142× (edge_ratio = (1-q)/q), so the real probability only has to clear
~1% to be +EV. Deliberately tiny clips (1/8 Kelly), spread across many cheap
buckets — most lose, the occasional hit pays for all of them.

Edge source: the crowd systematically underprices tail outcomes; the forecast
ensemble quantifies the tail better than the market does.
"""

from __future__ import annotations

from typing import List

from src.strategies.base_strategy import Signal, Strategy
from src.strategies.common import signal_meta
from src.market.snapshot import MarketSnapshot


class TailBucketSniper(Strategy):
    name = "tail_bucket_sniper"
    kelly_lambda = 0.125          # 1/8 Kelly — extreme variance, least-reliable tail q

    # tunables
    min_price: float = 0.003      # ignore sub-0.3¢ dust
    max_price: float = 0.15       # only "cheap" buckets
    ev_threshold: float = 0.02    # forecast q must beat price by >= this
    min_edge_ratio: float = 5.0   # require a fat payoff multiple

    def generate_signals(self, snapshot: MarketSnapshot) -> List[Signal]:
        out: List[Signal] = []
        fq = snapshot.forecast_q or {}
        for quote in snapshot.quotes:
            p = quote.yes_ask
            if not (self.min_price <= p <= self.max_price):
                continue
            q = fq.get(quote.label, 0.0)
            if q <= 0:
                continue
            ev = q - p
            edge_ratio = (1.0 - q) / q if q > 0 else float("inf")
            if ev < self.ev_threshold or edge_ratio < self.min_edge_ratio:
                continue
            meta = signal_meta(snapshot, quote, q=q, p=p, outcome="yes",
                               strategy=self.name)
            meta["edge_ratio"] = edge_ratio
            out.append(Signal(
                side="BUY", outcome="yes", target_price=p,
                strength=min(1.0, ev / 0.10),
                reason=f"tail YES {quote.label}: q={q:.3f} vs p={p:.3f} (edge_ratio {edge_ratio:.0f}x)",
                metadata=meta,
            ))
        return out
