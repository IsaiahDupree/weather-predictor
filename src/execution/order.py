"""
Unified Order / Fill dataclasses for prediction-market execution.

Reproduced from TradingBot (the same shape flows through sim → paper → live;
only the adapter behind the router changes), then specialized for binary
prediction markets:

  * ``AssetClass.PREDICTION`` is added.
  * ``limit_price`` is a probability in [0, 1] — the contract costs ``p`` and
    pays $1 if its outcome resolves YES, $0 otherwise.
  * ``metadata`` carries the prediction-market context (event ticker, bucket
    label, outcome side, the resolving station, q/EV at entry, strategy).

A "buy YES at p" is the canonical internal representation. Taking the NO view
is normalized at the adapter boundary: Kalshi flips to a no-side order, while
Polymarket buys the NO token id (recorded in ``metadata['token_id_no']``).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Outcome(str, Enum):
    """Which side of the binary contract the order expresses a view on."""
    YES = "yes"
    NO = "no"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    GTC = "gtc"          # good till cancelled
    GTD = "gtd"          # good till date
    IOC = "ioc"          # immediate or cancel (a.k.a. FAK)
    FOK = "fok"          # fill or kill


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AssetClass(str, Enum):
    PREDICTION = "prediction"


class Mode(str, Enum):
    SIM = "sim"
    PAPER = "paper"
    LIVE = "live"


@dataclass
class Order:
    """A single intent to buy/sell a binary outcome token.

    ``limit_price`` is in probability units [0, 1]. ``qty`` is the number of
    contracts/shares (each pays $1 on a winning resolution).
    """

    symbol: str                              # venue-native instrument id (ticker / clobTokenId)
    side: Side
    qty: float
    outcome: Outcome = Outcome.YES
    asset_class: AssetClass = AssetClass.PREDICTION
    order_type: OrderType = OrderType.LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: Optional[float] = None      # probability in [0, 1]
    broker: Optional[str] = None             # "kalshi" | "polymarket"; else router picks by venue
    mode: Mode = Mode.SIM
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    submitted_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Filled in by router/adapter on submission
    broker_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fills: List["Fill"] = field(default_factory=list)
    last_update: float = field(default_factory=time.time)
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.side, str):
            self.side = Side(self.side.lower())
        if isinstance(self.outcome, str):
            self.outcome = Outcome(self.outcome.lower())
        if isinstance(self.asset_class, str):
            self.asset_class = AssetClass(self.asset_class.lower())
        if isinstance(self.order_type, str):
            self.order_type = OrderType(self.order_type.lower())
        if isinstance(self.time_in_force, str):
            self.time_in_force = TimeInForce(self.time_in_force.lower())
        if isinstance(self.mode, str):
            self.mode = Mode(self.mode.lower())
        if isinstance(self.status, str):
            self.status = OrderStatus(self.status.lower())
        if self.limit_price is not None and not (0.0 <= self.limit_price <= 1.0):
            raise ValueError(
                f"limit_price must be a probability in [0,1], got {self.limit_price}"
            )
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")

    @property
    def notional(self) -> float:
        """Dollar capital at risk = price * qty (a binary contract can lose at most its cost)."""
        return float(self.limit_price or 0.0) * float(self.qty)

    def add_fill(self, fill: "Fill") -> None:
        if fill.outcome is None:
            fill.outcome = self.outcome
        self.fills.append(fill)
        new_qty = self.filled_qty + fill.qty
        if new_qty > 0:
            self.avg_fill_price = (
                (self.avg_fill_price * self.filled_qty + fill.price * fill.qty) / new_qty
            )
        self.filled_qty = new_qty
        self.last_update = time.time()
        if abs(self.filled_qty - self.qty) < 1e-9:
            self.status = OrderStatus.FILLED
        elif self.filled_qty > 0:
            self.status = OrderStatus.PARTIALLY_FILLED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "outcome": self.outcome.value,
            "qty": self.qty,
            "asset_class": self.asset_class.value,
            "order_type": self.order_type.value,
            "time_in_force": self.time_in_force.value,
            "limit_price": self.limit_price,
            "broker": self.broker,
            "mode": self.mode.value,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "notional": self.notional,
            "submitted_at": self.submitted_at,
            "last_update": self.last_update,
            "fills": [f.to_dict() for f in self.fills],
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class Fill:
    order_id: str
    price: float                  # probability in [0, 1]
    qty: float
    timestamp: float = field(default_factory=time.time)
    fee: float = 0.0
    venue: Optional[str] = None
    fill_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    outcome: Optional[Outcome] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "price": self.price,
            "qty": self.qty,
            "fee": self.fee,
            "venue": self.venue,
            "timestamp": self.timestamp,
            "outcome": self.outcome.value if self.outcome is not None else None,
        }


def resolution_pnl(entry_price: float, qty: float, won: bool, fee: float = 0.0) -> float:
    """PnL of a held position at market resolution.

    A winning share pays $1; a losing share pays $0. You paid ``entry_price``
    per share. So:  won → (1 - entry)*qty - fee ;  lost → -entry*qty - fee.
    """
    if won:
        return (1.0 - entry_price) * qty - fee
    return -entry_price * qty - fee
