"""
Expected value for binary prediction-market contracts.

Foundation (from the source article). A contract on outcome X costs ``p`` (its
market price, a probability in [0,1]) and pays $1 if X resolves true, $0
otherwise. With our estimate ``q`` of the true probability:

    EV per contract = q*(1 - p) - (1 - q)*p   ==   q - p

The simplified ``q - p`` and the full payout form are algebraically identical;
both are provided for clarity. We only trade when EV exceeds a threshold ``T``
that already nets out fees, half-spread, and a model-error buffer — and the EV
must survive the *executable* cost of sweeping the book, not just the touch.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple


def ev_full(q: float, p: float) -> float:
    """The full payout form: q*(1-p) - (1-q)*p. Equals q - p."""
    return q * (1.0 - p) - (1.0 - q) * p


def edge(q: float, p: float) -> float:
    """q - p — the per-contract expected profit (in $ per $1-payout contract)."""
    return q - p


def ev_buy(win_prob: float, cost: float) -> float:
    """EV of buying any outcome with true probability ``win_prob`` at ``cost``."""
    return win_prob - cost


def ev_yes(q: float, p_yes: float) -> float:
    """EV of buying YES (true prob q) at the YES price."""
    return q - p_yes


def ev_no(q: float, p_no: float) -> float:
    """EV of buying NO at the NO price. NO's true prob is (1 - q)."""
    return (1.0 - q) - p_no


def ev_per_dollar_staked(q: float, p: float) -> float:
    """Return on capital actually risked (you risk ``p`` per YES contract)."""
    if p <= 0:
        return 0.0
    return (q - p) / p


def executable_cost(
    ask_levels: Sequence[Tuple[float, float]],
    qty: float,
) -> Tuple[float, float, float]:
    """Sweep an ascending list of ``(price, size)`` ask levels to fill ``qty``.

    Returns ``(avg_price, filled_qty, total_cost)``. If the book is too thin we
    fill what's available and report the partial — the caller decides whether a
    partial fill at a worse average still clears EV. This is the difference
    between trading the *touch* (which lies) and the *executable* price.
    """
    remaining = qty
    cost = 0.0
    filled = 0.0
    for price, size in ask_levels:
        if remaining <= 0:
            break
        take = min(remaining, size)
        cost += take * price
        filled += take
        remaining -= take
    avg = (cost / filled) if filled > 0 else 0.0
    return avg, filled, cost


def ev_against_book(
    q: float,
    ask_levels: Sequence[Tuple[float, float]],
    qty: float,
    fee_per_contract: float = 0.0,
) -> Tuple[float, float, float]:
    """EV of buying ``qty`` by sweeping the book, net of per-contract fees.

    Returns ``(ev_per_contract, avg_price, filled_qty)``. ``ev_per_contract`` is
    ``q - avg_price - fee_per_contract`` over the filled quantity.
    """
    avg, filled, _ = executable_cost(ask_levels, qty)
    if filled <= 0:
        return -1.0, 0.0, 0.0
    return q - avg - fee_per_contract, avg, filled
