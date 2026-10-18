"""
ExecutionRouter — the single async submit() entrypoint for every order.

Responsibilities (reproduced from TradingBot):
  - Pre-trade risk gate (RiskEngine.check) — nothing reaches a venue ungated.
  - Idempotency: a client_order_id never fires twice.
  - Routing: pick the adapter by (mode, venue).
  - Replay log: append every order event to data/order_events.jsonl (redacted).
  - Portfolio snapshot for the risk engine, built from the chosen adapter.

The router is the *only* place that knows about adapters; strategies and the
loop speak the unified Order and never branch on venue.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from src.execution.adapters.base import BaseAdapter
from src.execution.order import Mode, Order, OrderStatus
from src.risk.risk_engine import RiskEngine

log = logging.getLogger(__name__)


class ExecutionRouter:
    def __init__(
        self,
        risk_engine: RiskEngine,
        event_log_path: Optional[Path] = None,
    ) -> None:
        self.risk_engine = risk_engine
        self._event_log_path = Path(event_log_path) if event_log_path else None
        self._adapters_by_mode_venue: Dict[tuple, BaseAdapter] = {}
        self._adapters_by_name: Dict[str, BaseAdapter] = {}
        self._orders: "OrderedDict[str, Order]" = OrderedDict()
        self._dedup_lock = Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_adapter(self, adapter: BaseAdapter, venue: Optional[str] = None) -> None:
        venue = venue or adapter.name
        self._adapters_by_name[adapter.name] = adapter
        for mode in adapter.supports_modes:
            self._adapters_by_mode_venue[(mode, venue)] = adapter

    def get_adapter(self, mode: str, venue: Optional[str]) -> Optional[BaseAdapter]:
        if venue and (mode, venue) in self._adapters_by_mode_venue:
            return self._adapters_by_mode_venue[(mode, venue)]
        # Fall back to any adapter for this mode (sim has no venue).
        for (m, _v), a in self._adapters_by_mode_venue.items():
            if m == mode:
                return a
        if venue:
            return self._adapters_by_name.get(venue)
        return None

    def registered(self) -> Dict[str, Dict[str, Any]]:
        return {name: a.health() for name, a in self._adapters_by_name.items()}

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        order: Order,
        ref_price: Optional[float] = None,
        *,
        event_ticker: Optional[str] = None,
        resolution_known: bool = True,
        signal_age_s: Optional[float] = None,
        vol_gate_pass: Optional[bool] = None,
        book_depth: Optional[float] = None,
        is_arbitrage: bool = False,
    ) -> Order:
        # Idempotency
        with self._dedup_lock:
            existing = self._orders.get(order.client_order_id)
            if existing is not None:
                return existing
            self._orders[order.client_order_id] = order

        if ref_price is not None:
            order.metadata.setdefault("ref_price", ref_price)
        # Stamp event attribution onto the order so the adapter's position book
        # (and thus the per-event exposure snapshot) can group by event.
        if event_ticker and not order.metadata.get("event_ticker"):
            order.metadata["event_ticker"] = event_ticker

        venue = order.broker
        adapter = self.get_adapter(order.mode.value, venue)
        if adapter is None or not adapter.is_available():
            order.status = OrderStatus.REJECTED
            order.error = f"no available adapter for mode={order.mode.value} venue={venue}"
            self._log_event("rejected_no_adapter", order)
            return order

        # Pre-trade risk gate
        gate_price = ref_price or order.limit_price or 0.0
        portfolio = await self._snapshot_portfolio(adapter)
        check = self.risk_engine.check(
            symbol=order.symbol,
            qty=order.qty,
            price=gate_price,
            portfolio=portfolio,
            event_ticker=event_ticker or (order.metadata or {}).get("event_ticker"),
            resolution_known=resolution_known,
            signal_age_s=signal_age_s,
            vol_gate_pass=vol_gate_pass,
            book_depth=book_depth,
            is_arbitrage=is_arbitrage,
            side=order.side.value,
        )
        if not check.ok:
            order.status = OrderStatus.REJECTED
            order.error = f"{check.code}: {check.message}"
            order.metadata["risk_code"] = check.code
            order.metadata["risk_details"] = check.details
            self._log_event("rejected_risk", order)
            return order

        self._log_event("submitting", order)
        try:
            await adapter.submit(order)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"adapter exception: {exc}"
            log.exception("[router] adapter submit failed")

        self._log_event(f"status_{order.status.value}", order)
        return order

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_order(self, client_order_id: str) -> Optional[Order]:
        return self._orders.get(client_order_id)

    def list_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        items = list(self._orders.values())[-limit:]
        return [o.to_dict() for o in reversed(items)]

    async def get_positions(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for (m, _v), adapter in self._adapters_by_mode_venue.items():
            if mode and m != mode:
                continue
            if adapter.name in seen:
                continue
            seen.add(adapter.name)
            try:
                for p in await adapter.get_positions():
                    p["venue"] = adapter.name
                    out.append(p)
            except Exception:
                log.exception("[router] get_positions on %s failed", adapter.name)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _snapshot_portfolio(self, adapter: BaseAdapter) -> Dict[str, Any]:
        """Build the portfolio dict the risk engine needs from an adapter."""
        equity = 0.0
        exposure_total = 0.0
        exposure_by_event: Dict[str, float] = {}
        # SimAdapter exposes equity() directly; otherwise approximate from positions.
        if hasattr(adapter, "equity"):
            try:
                equity = float(adapter.equity())
            except Exception:
                equity = 0.0
        try:
            positions = await adapter.get_positions()
        except Exception:
            positions = []
        for p in positions:
            notional = abs(float(p.get("qty", 0))) * float(p.get("avg_price", 0))
            exposure_total += notional
            ev = p.get("event_ticker") or p.get("event")
            if ev:
                exposure_by_event[ev] = exposure_by_event.get(ev, 0.0) + notional
        if equity == 0.0:
            equity = exposure_total  # best-effort for venues without an equity readout
        return {
            "equity": equity,
            "exposure_total": exposure_total,
            "exposure_by_event": exposure_by_event,
            "positions": positions,
        }

    def _log_event(self, event: str, order: Order) -> None:
        if self._event_log_path is None:
            return
        try:
            self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.time(), "event": event, "order": order.to_dict()}
            with self._event_log_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            log.exception("[router] replay log write failed")
