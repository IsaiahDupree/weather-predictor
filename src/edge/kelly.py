"""
Position sizing — Kelly, fractional Kelly, and the article's "fixed clip scaled
by edge width".

For a binary contract bought at price ``p`` (the amount staked per $1 of payoff)
with true win probability ``q``, the Kelly-optimal fraction of bankroll is:

    f* = (q - p) / (1 - p)          (YES)

which is exactly EV / (amount-at-risk-per-unit-payoff). Full Kelly is too
aggressive given ``q`` is an estimate, so every strategy applies a fractional
multiplier ``lam`` in [1/8, 1/2]. The bot also supports the sizing real
high-PnL bots actually use — a small fixed clip scaled by how wide the edge is,
capped — because in thin niche markets a large order moves the price and
destroys the edge it was trying to capture.
"""

from __future__ import annotations


def kelly_fraction(win_prob: float, cost: float) -> float:
    """Full-Kelly bankroll fraction for buying an outcome at ``cost``.

    f* = (win_prob - cost) / (1 - cost). Clamped to [0, 1]; returns 0 when there
    is no edge (never bet a non-positive-edge contract).
    """
    if cost >= 1.0 or cost < 0.0:
        return 0.0
    f = (win_prob - cost) / (1.0 - cost)
    return max(0.0, min(1.0, f))


def kelly_yes(q: float, p: float) -> float:
    return kelly_fraction(q, p)


def kelly_no(q: float, p_no: float) -> float:
    """Kelly for buying NO: win prob is (1 - q), cost is the NO price."""
    return kelly_fraction(1.0 - q, p_no)


def fractional(full_kelly: float, lam: float) -> float:
    """Scale full Kelly by ``lam`` (e.g. 1/2, 1/4, 1/8)."""
    return max(0.0, lam) * max(0.0, full_kelly)


def edge_ratio(q: float) -> float:
    """(1 - q)/q — the payoff multiple of a winning cheap bet.

    At q = 0.007 this is ~142×: a winning tail bet returns 142× its stake, which
    is why a sub-1% true probability can still be wildly +EV at a 0.7¢ price.
    """
    if q <= 0:
        return float("inf")
    return (1.0 - q) / q


def fixed_scaled(
    ev: float,
    base_clip: float,
    threshold: float = 0.0,
    width: float = 0.20,
    cap_mult: float = 3.0,
) -> float:
    """The article's "fixed size scaled by edge width", in dollars.

        stake = base_clip * clamp((ev - threshold) / width, 0, cap_mult)

    Wider EV gaps → proportionally bigger clips, but every clip stays small and
    capped at ``cap_mult * base_clip``. Below threshold → zero.
    """
    if ev <= threshold or base_clip <= 0:
        return 0.0
    mult = (ev - threshold) / width if width > 0 else cap_mult
    mult = max(0.0, min(mult, cap_mult))
    return base_clip * mult


def size_position(
    *,
    q: float,
    price: float,
    bankroll: float,
    lam: float,
    base_clip: float,
    ev_threshold: float,
    max_order_notional: float,
    liquidity_cap_notional: float,
    ev_width: float = 0.20,
) -> float:
    """Combine all sizing rules and return the final dollar notional to deploy.

    Takes the **minimum** of fractional-Kelly notional, fixed-scaled notional,
    the per-trade cap, and the liquidity cap — the most conservative wins. This
    is the single sizing entry point the runtime loop calls.
    """
    ev = q - price
    if ev <= ev_threshold:
        return 0.0
    fk = fractional(kelly_fraction(q, price), lam)
    kelly_notional = fk * bankroll
    scaled_notional = fixed_scaled(ev, base_clip, ev_threshold, ev_width)
    return max(
        0.0,
        min(kelly_notional, scaled_notional, max_order_notional, liquidity_cap_notional),
    )
