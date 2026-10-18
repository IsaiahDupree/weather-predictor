"""
Abstract base for all weather prediction-market strategies.

A strategy is a pure decision function: given a ``MarketSnapshot`` (assembled by
the runtime loop from market prices + raw obs + forecast distribution + the
resolution spec), it returns zero or more ``Signal`` objects. It never touches
the network or the router — that keeps strategies trivially unit-testable and
lets the loop apply uniform risk gating / sizing to every signal.

The ``Signal`` carries the decision context the loop needs to size and gate the
order: ``q`` (our probability), ``p`` (market price), ``ev``, the suggested
Kelly fraction, and whether the volatility gate passed.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.execution.order import Outcome


@dataclass
class Signal:
    """A strategy's intent to take (or skip) a position.

    side     : 'BUY' to open, 'HOLD' to do nothing. (Exits are modelled as a
               BUY of the opposing outcome or handled by the settlement engine;
               prediction-market shares are typically held to resolution.)
    outcome  : YES or NO — which outcome token to buy.
    strength : [0,1] — confidence used by fixed-scaled sizing (clamp(EV/W,0,1)).
    target_price : the limit price (probability) to bid.
    """

    side: str                       # 'BUY' | 'HOLD'
    outcome: Outcome = Outcome.YES
    strength: float = 1.0
    target_price: Optional[float] = None
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "HOLD"):
            raise ValueError(f"Invalid signal side: {self.side}")
        if isinstance(self.outcome, str):
            self.outcome = Outcome(self.outcome.lower())
        self.strength = max(0.0, min(1.0, self.strength))

    @property
    def is_actionable(self) -> bool:
        return self.side == "BUY" and self.strength > 0.0

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "outcome": self.outcome.value,
            "strength": self.strength,
            "target_price": self.target_price,
            "reason": self.reason,
            "metadata": self.metadata,
        }


HOLD = Signal(side="HOLD", strength=0.0, reason="no signal")


class Strategy(abc.ABC):
    """Abstract base for weather prediction-market strategies.

    Subclasses implement ``generate_signals(snapshot)``. ``name`` identifies the
    strategy in logs and on each order's metadata. ``kelly_lambda`` is the
    fractional-Kelly multiplier the loop applies to this strategy's signals
    (tail bets use a small lambda; gated scalps use up to 1/2).
    """

    name: str = "base"
    kelly_lambda: float = 0.25       # fraction of full Kelly this strategy is sized at

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = dict(params)
        for k, v in params.items():
            if hasattr(self, k):
                setattr(self, k, v)

    @abc.abstractmethod
    def generate_signals(self, snapshot: Any) -> List[Signal]:
        """Return zero or more Signals for the given MarketSnapshot."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Strategy {self.name} lambda={self.kelly_lambda}>"
