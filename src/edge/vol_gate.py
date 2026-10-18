"""
Volatility gate — the line between safe near-expiry scalping and picking up
pennies in front of a steamroller.

The near-expiry scalp buys a 95–99¢ near-certain outcome to collect the last
few cents. That is only safe when the outcome is *genuinely* near-certain, not
merely *priced* that way. The gate requires the current margin to the resolving
threshold to dwarf the remaining possible move:

    passes  ⇔  margin_to_threshold >= k * sigma_remaining        (k >= 3)

If it fails, the trade is SKIPPED regardless of how attractive the price looks.
One 99¢→0 flip wipes out the gains from ~99 winning scalps, so this gate is
non-negotiable.
"""

from __future__ import annotations


def passes(margin_to_threshold: float, sigma_remaining: float, k: float = 3.0) -> bool:
    """True if it's safe to scalp: margin dwarfs the remaining uncertainty.

    margin_to_threshold : how far the resolved value already is past the line
                          that decides the outcome (in the same units as sigma).
                          Must be positive (we're on the winning side).
    sigma_remaining     : std-dev of the still-possible move before resolution.
    k                   : safety multiple (>= 3 recommended).
    """
    if margin_to_threshold <= 0:
        return False
    if sigma_remaining <= 0:
        return True  # nothing left to move
    return margin_to_threshold >= k * sigma_remaining


def safety_margin(margin_to_threshold: float, sigma_remaining: float) -> float:
    """How many sigmas of cushion we have (margin / sigma). Higher = safer."""
    if sigma_remaining <= 0:
        return float("inf")
    return margin_to_threshold / sigma_remaining
