"""
Shared helpers for the strategies — nowcast projection and signal-metadata
assembly — so each strategy file stays focused on its own edge logic.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from src.edge.distribution import high_market_bucket_probs
from src.edge.kelly import kelly_fraction
from src.edge.nowcast import project
from src.market.snapshot import BucketQuote, MarketSnapshot


def project_high(snap: MarketSnapshot) -> Tuple[Optional[float], Optional[float]]:
    """Project the day's final HIGH (mu) and its uncertainty (sigma) from obs.

    Only warming (positive trend) raises a daily high, so a negative trend is
    clamped to zero — the high already recorded can't be undone.
    """
    t_now = snap.obs_temp if snap.obs_temp is not None else snap.daily_high_so_far
    if t_now is None:
        return None, None
    trend = max(0.0, snap.trend_per_hr or 0.0)
    mu, sigma = project(t_now, trend, snap.hours_to_resolution, snap.resid_sigma)
    if snap.daily_high_so_far is not None:
        mu = max(mu, snap.daily_high_so_far)
    return mu, sigma


def nowcast_q(snap: MarketSnapshot) -> Dict[str, float]:
    """Per-bucket probability from the live obs nowcast (monotonic high market)."""
    mu, sigma = project_high(snap)
    if mu is None:
        return {}
    return high_market_bucket_probs(snap.daily_high_so_far, mu, sigma, snap.grid, floor=0.001)


def signal_meta(
    snap: MarketSnapshot,
    quote: BucketQuote,
    *,
    q: float,
    p: float,
    outcome: str,
    strategy: str,
    vol_gate_pass: bool = True,
    is_arbitrage: bool = False,
    book_depth: Optional[float] = None,
) -> Dict[str, object]:
    """Assemble the metadata the runtime loop needs to size + gate an order."""
    cost = p
    win_prob = q if outcome == "yes" else (1.0 - q)
    return {
        "strategy": strategy,
        "event_ticker": snap.event_ticker,
        "venue": snap.venue,
        "city": snap.city,
        "symbol": quote.symbol,
        "bucket_label": quote.label,
        "outcome": outcome,
        "token_id_no": quote.token_id_no,
        "q": q,
        "p": p,
        "ev": win_prob - cost,
        "kelly_f": kelly_fraction(win_prob, cost),
        "vol_gate_pass": vol_gate_pass,
        "is_arbitrage": is_arbitrage,
        "book_depth": book_depth,
        "obs_age_s": snap.obs_age_s,
        "seconds_to_resolution": snap.seconds_to_resolution,
    }
