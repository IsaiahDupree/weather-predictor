"""
Pre-trade risk engine.

Every order passes through ``RiskEngine.check()`` before submission. Rejections
carry a stable ``code`` so the loop can log/surface them. Reproduces TradingBot's
gates (HALTED, notional, order-rate, daily-loss auto-halt, exposure) and adds the
prediction-market-specific gates from the design:

  VOL_GATE            — refuse near-certain scalps unless margin >= k*sigma.
  STALE_SIGNAL        — drop signals older than their TTL (oracle-lag edges decay).
  LIQUIDITY           — order size must not exceed a fraction of book depth.
  EVENT_CONCENTRATION — cap exposure per event.
  FORBIDDEN_EVENT     — reject any market whose resolution station is unknown.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits

log = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    ok: bool
    code: str = "ok"
    message: str = ""
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message,
                "details": self.details or {}}


class RiskEngine:
    def __init__(self, limits: Optional[RiskLimits] = None,
                 kill_switch: Optional[KillSwitch] = None) -> None:
        self.limits = limits or RiskLimits()
        self.kill_switch = kill_switch
        self._order_times: Deque[float] = deque(maxlen=2000)
        self._day_start_equity: Optional[float] = None
        self._day_start_date: Optional[str] = None
        self._last_rejection: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return bool(self.kill_switch and self.kill_switch.is_tripped())

    def _maybe_roll_day(self, equity: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_equity = equity

    # ------------------------------------------------------------------

    def check(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        portfolio: Dict[str, Any],
        event_ticker: Optional[str] = None,
        resolution_known: bool = True,
        signal_age_s: Optional[float] = None,
        vol_gate_pass: Optional[bool] = None,
        book_depth: Optional[float] = None,
        is_arbitrage: bool = False,
    ) -> RiskCheckResult:
        """Validate an order against limits + portfolio state. ``portfolio`` keys:
        ``equity``, ``exposure_total`` ($), ``exposure_by_event`` ({event: $})."""
        if not self.limits.enabled:
            return self._ok()

        if self.halted:
            reason = self.kill_switch.reason if self.kill_switch else "kill switch engaged"
            return self._reject("HALTED", f"Trading halted: {reason}")

        if qty <= 0:
            return self._reject("INVALID_QTY", "Order quantity must be positive")
        if not (0.0 < price <= 1.0):
            return self._reject("INVALID_PRICE", f"Price must be in (0,1], got {price}")

        if not resolution_known:
            return self._reject(
                "FORBIDDEN_EVENT",
                f"{symbol}: resolution station unknown — refusing to trade an "
                f"event we can't map to a known (venue,city) station",
                {"symbol": symbol},
            )

        notional = qty * price

        if notional > self.limits.max_order_notional:
            return self._reject(
                "ORDER_NOTIONAL",
                f"Order notional ${notional:,.2f} exceeds max ${self.limits.max_order_notional:,.2f}",
                {"notional": notional, "limit": self.limits.max_order_notional},
            )

        # Rolling order-rate (last 60s)
        now = time.monotonic()
        cutoff = now - 60.0
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()
        if len(self._order_times) >= self.limits.max_orders_per_minute:
            return self._reject(
                "ORDER_RATE",
                f"Order rate exceeds {self.limits.max_orders_per_minute}/min",
                {"recent_orders": len(self._order_times)},
            )

        # Daily-loss auto-halt
        equity = float(portfolio.get("equity", 0.0) or 0.0)
        self._maybe_roll_day(equity)
        if self._day_start_equity is not None and self._day_start_equity > 0:
            day_pnl = equity - self._day_start_equity
            if day_pnl <= -abs(self.limits.daily_loss_cap):
                msg = (f"Daily loss ${-day_pnl:,.2f} exceeds cap "
                       f"${self.limits.daily_loss_cap:,.2f}")
                if self.kill_switch and not self.kill_switch.is_tripped():
                    self.kill_switch.trip(f"DAILY_LOSS: {msg}")
                return self._reject("DAILY_LOSS", msg,
                                    {"day_pnl": day_pnl, "day_start_equity": self._day_start_equity})

        # Stale-signal gate
        if signal_age_s is not None and signal_age_s > self.limits.max_signal_age_s:
            return self._reject(
                "STALE_SIGNAL",
                f"Signal age {signal_age_s:.0f}s exceeds max {self.limits.max_signal_age_s:.0f}s",
                {"signal_age_s": signal_age_s},
            )

        # Volatility gate (near-expiry scalps). Arbitrage is exempt (risk-free).
        if vol_gate_pass is False and not is_arbitrage:
            return self._reject(
                "VOL_GATE",
                "Volatility gate failed: margin to threshold < k*sigma_remaining",
                {},
            )

        # Liquidity gate — order must not exceed a fraction of top-of-book depth.
        if book_depth is not None and book_depth > 0:
            if qty > self.limits.liquidity_depth_frac * book_depth:
                return self._reject(
                    "LIQUIDITY",
                    f"Order qty {qty:g} exceeds {self.limits.liquidity_depth_frac:.0%} "
                    f"of book depth {book_depth:g}",
                    {"qty": qty, "book_depth": book_depth},
                )

        # Event-concentration gate
        if event_ticker:
            exposure_by_event = portfolio.get("exposure_by_event", {}) or {}
            current = float(exposure_by_event.get(event_ticker, 0.0) or 0.0)
            if current + notional > self.limits.max_position_per_event:
                return self._reject(
                    "EVENT_CONCENTRATION",
                    f"Event {event_ticker} exposure ${current + notional:,.2f} exceeds "
                    f"max ${self.limits.max_position_per_event:,.2f}",
                    {"event": event_ticker, "current": current, "add": notional},
                )

        # Total-exposure gate
        exposure_total = float(portfolio.get("exposure_total", 0.0) or 0.0)
        if exposure_total + notional > self.limits.max_total_exposure:
            return self._reject(
                "TOTAL_EXPOSURE",
                f"Total exposure ${exposure_total + notional:,.2f} exceeds "
                f"max ${self.limits.max_total_exposure:,.2f}",
                {"exposure_total": exposure_total, "add": notional,
                 "limit": self.limits.max_total_exposure},
            )

        self._order_times.append(now)
        return RiskCheckResult(ok=True, code="ok", message="approved",
                               details={"notional": notional})

    # ------------------------------------------------------------------

    def _ok(self) -> RiskCheckResult:
        return RiskCheckResult(ok=True, code="ok", message="approved", details={})

    def _reject(self, code: str, message: str,
                details: Optional[Dict[str, Any]] = None) -> RiskCheckResult:
        result = RiskCheckResult(ok=False, code=code, message=message, details=details or {})
        self._last_rejection = result.to_dict()
        return result

    def last_rejection(self) -> Optional[Dict[str, Any]]:
        return self._last_rejection
