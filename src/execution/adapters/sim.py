"""
SimAdapter — in-memory matching + settlement engine for binary contracts.

Fills orders at the supplied reference price (``order.metadata['ref_price']``)
or a price callback, tracks a cash + positions book, and — crucially for
prediction markets — settles positions to **$1 (won) or $0 (lost)** when a
market resolves. This is what makes SIM mode a faithful dry-run of the full
buy→hold→resolve lifecycle without touching a venue.

Used for SIM mode, unit tests, and the SIM→PAPER→LIVE promotion harness.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.execution.adapters.base import BaseAdapter
from src.execution.order import Fill, Order, OrderStatus, OrderType, Outcome, Side


@dataclass
class _Position:
    qty: float = 0.0
    avg_price: float = 0.0           # avg entry price (probability)
    outcome: Outcome = Outcome.YES
    event_ticker: Optional[str] = None   # for per-event exposure attribution


@dataclass
class SettlementRecord:
    symbol: str
    qty: float
    entry_price: float
    won: bool
    pnl: float
    ts: float = field(default_factory=time.time)


class SimAdapter(BaseAdapter):
    name = "sim"
    asset_classes = ("prediction",)
    supports_modes = ("sim",)

    def __init__(
        self,
        starting_cash: float = 1000.0,
        price_lookup: Optional[Callable[[str], Optional[float]]] = None,
        fee_per_contract: float = 0.0,
    ) -> None:
        self._cash = starting_cash
        self._starting_cash = starting_cash
        self._positions: Dict[str, _Position] = {}
        self._price_lookup = price_lookup
        self._fee_per_contract = fee_per_contract
        self._settlements: List[SettlementRecord] = []
        self._realized_pnl = 0.0
        self._lock = asyncio.Lock()

    def is_available(self) -> bool:
        return True

    # ------------------------------------------------------------------

    async def submit(self, order: Order) -> Order:
        async with self._lock:
            order.broker = self.name
            order.broker_order_id = str(uuid.uuid4())
            order.status = OrderStatus.SUBMITTED

            ref_price = self._reference_price(order)
            if ref_price is None or not (0.0 < ref_price <= 1.0):
                order.status = OrderStatus.REJECTED
                order.error = "no valid reference price (must be in (0,1])"
                return order

            if order.order_type == OrderType.LIMIT and order.limit_price is not None:
                lp = float(order.limit_price)
                marketable = (order.side == Side.BUY and ref_price <= lp) or \
                             (order.side == Side.SELL and ref_price >= lp)
                if not marketable:
                    # Resting limit not crossed — leave unfilled (caller may cancel/re-quote).
                    order.status = OrderStatus.SUBMITTED
                    return order
                fill_price = lp  # price improvement to the limit, like a real book
            else:
                fill_price = ref_price

            self._fill(order, fill_price)
            return order

    def _reference_price(self, order: Order) -> Optional[float]:
        ref = (order.metadata or {}).get("ref_price")
        if ref:
            return float(ref)
        if order.limit_price:
            return float(order.limit_price)
        if self._price_lookup:
            try:
                p = self._price_lookup(order.symbol)
                return float(p) if p else None
            except Exception:
                return None
        return None

    def _fill(self, order: Order, price: float) -> None:
        fee = self._fee_per_contract * order.qty
        notional = order.qty * price
        if order.side == Side.BUY:
            if self._cash < notional + fee:
                order.status = OrderStatus.REJECTED
                order.error = "insufficient cash"
                return
            self._cash -= notional + fee
            pos = self._positions.setdefault(order.symbol, _Position(outcome=order.outcome))
            new_qty = pos.qty + order.qty
            pos.avg_price = ((pos.qty * pos.avg_price) + (order.qty * price)) / new_qty if new_qty else price
            pos.qty = new_qty
            pos.outcome = order.outcome
            pos.event_ticker = (order.metadata or {}).get("event_ticker") or pos.event_ticker
        else:  # SELL — close/reduce a position before resolution
            pos = self._positions.get(order.symbol)
            if pos is None or pos.qty < order.qty - 1e-9:
                order.status = OrderStatus.REJECTED
                order.error = "cannot sell more than held"
                return
            self._cash += notional - fee
            self._realized_pnl += (price - pos.avg_price) * order.qty
            pos.qty -= order.qty
            if pos.qty <= 1e-9:
                self._positions.pop(order.symbol, None)

        order.add_fill(Fill(order_id=order.broker_order_id or "", price=price,
                            qty=order.qty, fee=fee, venue=self.name, outcome=order.outcome))

    # ------------------------------------------------------------------
    # Settlement — the prediction-market-specific lifecycle step
    # ------------------------------------------------------------------

    async def resolve(self, symbol: str, won: bool) -> Optional[SettlementRecord]:
        """Settle a held position at resolution: each share pays $1 if won, $0 else."""
        async with self._lock:
            pos = self._positions.pop(symbol, None)
            if pos is None or pos.qty <= 0:
                return None
            payout = pos.qty * (1.0 if won else 0.0)
            self._cash += payout
            pnl = (1.0 - pos.avg_price) * pos.qty if won else -pos.avg_price * pos.qty
            self._realized_pnl += pnl
            rec = SettlementRecord(symbol=symbol, qty=pos.qty, entry_price=pos.avg_price,
                                   won=won, pnl=pnl)
            self._settlements.append(rec)
            return rec

    async def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        return {"ok": True, "note": "sim fills/rejects synchronously; nothing resting to cancel"}

    async def cancel_all(self) -> List[Dict[str, Any]]:
        return [{"ok": True}]

    async def flatten(self) -> List[Dict[str, Any]]:
        """Mark out every open position at its last/avg price (pre-resolution exit)."""
        results: List[Dict[str, Any]] = []
        async with self._lock:
            for sym, pos in list(self._positions.items()):
                mark = pos.avg_price
                if self._price_lookup:
                    try:
                        mark = float(self._price_lookup(sym) or pos.avg_price)
                    except Exception:
                        mark = pos.avg_price
                self._cash += pos.qty * mark
                self._realized_pnl += (mark - pos.avg_price) * pos.qty
                results.append({"symbol": sym, "qty": pos.qty, "mark": mark})
                self._positions.pop(sym, None)
        return results

    async def get_positions(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return [
                {"symbol": s, "qty": p.qty, "avg_price": p.avg_price,
                 "outcome": p.outcome.value, "event_ticker": p.event_ticker}
                for s, p in self._positions.items() if p.qty > 0
            ]

    # ------------------------------------------------------------------
    # Inspection (tests / dashboard)
    # ------------------------------------------------------------------

    def get_cash(self) -> float:
        return self._cash

    def equity(self) -> float:
        """Cash + mark-to-avg value of open positions."""
        held = sum(p.qty * p.avg_price for p in self._positions.values())
        return self._cash + held

    def realized_pnl(self) -> float:
        return self._realized_pnl

    def settlements(self) -> List[SettlementRecord]:
        return list(self._settlements)
