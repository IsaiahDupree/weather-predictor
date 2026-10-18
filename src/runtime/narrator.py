"""
Narrator — structured, human-readable logging for the loop, plus a JSONL trail.

Keeps the console legible (one line per decision) while writing a machine-
readable record of every signal/order/skip to data/decisions.jsonl for later
calibration (Brier score of q vs outcomes, EV-weighted PnL attribution).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("weatherbot")


class Narrator:
    def __init__(self, log_dir: Path) -> None:
        self.path = Path(log_dir) / "decisions.jsonl"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _write(self, kind: str, payload: Dict[str, Any]) -> None:
        row = {"ts": time.time(), "kind": kind, **payload}
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    def signal(self, snap_venue: str, strategy: str, reason: str, meta: Dict[str, Any]) -> None:
        log.info("[signal] %s/%s :: %s", snap_venue, strategy, reason)
        self._write("signal", {"venue": snap_venue, "strategy": strategy, "reason": reason, "meta": meta})

    def order(self, action: str, symbol: str, outcome: str, qty: float, price: float,
              status: str, detail: Optional[str] = None) -> None:
        log.info("[order] %s %s %s qty=%.4g @ %.3f -> %s%s",
                 action, symbol, outcome, qty, price, status,
                 f" ({detail})" if detail else "")
        self._write("order", {"action": action, "symbol": symbol, "outcome": outcome,
                              "qty": qty, "price": price, "status": status, "detail": detail})

    def skip(self, symbol: str, reason: str, meta: Optional[Dict[str, Any]] = None) -> None:
        log.debug("[skip] %s :: %s", symbol, reason)
        self._write("skip", {"symbol": symbol, "reason": reason, "meta": meta or {}})

    def note(self, msg: str, **kw: Any) -> None:
        log.info("[note] %s", msg)
        self._write("note", {"msg": msg, **kw})
