"""
Abstract venue adapter — sim / paper / live implementations subclass this.

Reproduced from TradingBot. The adapter's only job is to translate a unified
``Order`` into a venue-specific submission and report fills back. The router
owns risk checks, idempotency, and the replay log, so adapters stay thin.

Subclasses must override ``submit``. ``cancel``, ``cancel_all``, ``flatten``,
``get_positions`` have safe defaults but should be implemented for live use.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.execution.order import Order


class BaseAdapter:
    name: str = "base"
    asset_classes: tuple = ("prediction",)
    supports_modes: tuple = ()      # subset of ("sim", "paper", "live")

    def is_available(self) -> bool:
        """True if credentials/config are present and the adapter can submit."""
        return True

    async def submit(self, order: Order) -> Order:
        """Submit an order. Mutates and returns the same Order (status, fills)."""
        raise NotImplementedError

    async def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        return {"ok": False, "error": "cancel not implemented"}

    async def cancel_all(self) -> List[Dict[str, Any]]:
        return [{"ok": False, "error": "cancel_all not implemented"}]

    async def flatten(self) -> List[Dict[str, Any]]:
        return [{"ok": False, "error": "flatten not implemented"}]

    async def get_positions(self) -> List[Dict[str, Any]]:
        return []

    def supports_mode(self, mode: str) -> bool:
        return mode in self.supports_modes

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.is_available(),
            "asset_classes": list(self.asset_classes),
            "modes": list(self.supports_modes),
        }

    async def probe(self) -> Dict[str, Any]:
        """Read-only sanity check against the venue (used before live trading)."""
        if not self.is_available():
            return {"name": self.name, "ok": False, "error": "not available (no creds / init failed)"}
        try:
            positions = await self.get_positions()
            return {
                "name": self.name, "ok": True,
                "n_positions": len(positions or []),
                "modes": list(self.supports_modes),
            }
        except Exception as exc:
            return {"name": self.name, "ok": False, "error": str(exc)}
