"""
Near-expiry scalp — collect the last few cents on a near-certain outcome.

Close to resolution, buy a YES (or NO) trading at 90–99¢ when the raw obs has
all but locked the outcome, pocketing the residual 1–10¢ as it settles to $1.
Small per trade, but at volume across many events it compounds.

The whole strategy lives or dies on the volatility gate: a 99¢→0 flip wipes out
~99 winning scalps. So it only fires when the margin to the deciding threshold
dwarfs the remaining possible move (margin >= k*sigma). High volatility ⇒ skip,
no matter how attractive the price.

Edge source: most participants won't touch a 96¢ outcome (upside looks thin);
the bot harvests certainty-adjusted volume that humans leave on the table.
"""

from __future__ import annotations

from typing import List

from src.edge.vol_gate import passes as vol_passes, safety_margin
from src.strategies.base_strategy import Signal, Strategy
from src.strategies.common import nowcast_q, project_high, signal_meta
from src.market.snapshot import MarketSnapshot


class NearExpiryScalp(Strategy):
    name = "near_expiry_scalp"
    kelly_lambda = 0.5            # high p, but only when the vol gate passes

    # tunables
    window_s: float = 1800.0      # only active within 30 min of resolution
    min_price: float = 0.90
    max_price: float = 0.99       # never pay 1.00; leave a cushion
    min_q: float = 0.97           # outcome must be genuinely near-certain
    vol_k: float = 3.0

    def generate_signals(self, snapshot: MarketSnapshot) -> List[Signal]:
        out: List[Signal] = []
        if snapshot.seconds_to_resolution > self.window_s:
            return out
        q_map = nowcast_q(snapshot)
        if not q_map:
            return out
        _, sigma = project_high(snapshot)
        sigma = sigma if sigma is not None else snapshot.resid_sigma
        high = snapshot.daily_high_so_far if snapshot.daily_high_so_far is not None else snapshot.obs_temp
        if high is None:
            return out

        for quote in snapshot.quotes:
            q = q_map.get(quote.label, 0.0)
            # YES side: bucket near-certain to win, priced 0.90-0.99.
            if q >= self.min_q and self.min_price <= quote.yes_ask <= self.max_price:
                margin = float("inf") if quote.bucket.hi is None else (quote.bucket.hi - high)
                gate_ok = vol_passes(margin, sigma, self.vol_k)
                if gate_ok and quote.yes_ask < q:
                    meta = signal_meta(snapshot, quote, q=q, p=quote.yes_ask, outcome="yes",
                                       strategy=self.name, vol_gate_pass=gate_ok)
                    meta["safety_sigmas"] = safety_margin(margin, sigma)
                    out.append(Signal(
                        side="BUY", outcome="yes", target_price=quote.yes_ask,
                        strength=min(1.0, (q - quote.yes_ask) / 0.05),
                        reason=f"scalp YES {quote.label} @ {quote.yes_ask:.2f} (q={q:.3f}, "
                               f"{safety_margin(margin, sigma):.1f}σ cushion)",
                        metadata=meta,
                    ))
            # NO side: bucket near-certain to LOSE, NO priced 0.90-0.99.
            q_no = 1.0 - q
            if q_no >= self.min_q and self.min_price <= quote.no_ask <= self.max_price:
                # NO wins unless the high lands in [lo,hi). Margin = how far the
                # high already is past hi (can't come back down), or below lo.
                if quote.bucket.hi is not None and high >= quote.bucket.hi:
                    margin = high - quote.bucket.hi
                elif quote.bucket.lo is not None and high < quote.bucket.lo:
                    margin = quote.bucket.lo - high
                else:
                    margin = 0.0  # high currently inside the bucket — not safe for NO
                gate_ok = vol_passes(margin, sigma, self.vol_k)
                if gate_ok and quote.no_ask < q_no:
                    meta = signal_meta(snapshot, quote, q=q, p=quote.no_ask, outcome="no",
                                       strategy=self.name, vol_gate_pass=gate_ok)
                    meta["safety_sigmas"] = safety_margin(margin, sigma)
                    out.append(Signal(
                        side="BUY", outcome="no", target_price=quote.no_ask,
                        strength=min(1.0, (q_no - quote.no_ask) / 0.05),
                        reason=f"scalp NO {quote.label} @ {quote.no_ask:.2f} (q_no={q_no:.3f})",
                        metadata=meta,
                    ))
        return out
