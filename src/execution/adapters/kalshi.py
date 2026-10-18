"""
KalshiAdapter — live/paper execution adapter for the Kalshi prediction market.

Translates a unified :class:`~src.execution.order.Order` into a Kalshi
``CreateOrder`` request, reports fills + taker fees back onto the order, and
exposes cancel / position queries used by the router.

Environments (Kalshi runs the *demo* exchange on fake money — that is our
PAPER tier; the *elections* host is real money LIVE):

  * PAPER -> https://demo-api.kalshi.co/trade-api/v2          (demo, fake money)
  * LIVE  -> https://api.elections.kalshi.com/trade-api/v2    (production, real)

Authentication is RSA-PSS request signing (no session tokens). Each request
carries three headers:

  * ``KALSHI-ACCESS-KEY``       — the API key id
  * ``KALSHI-ACCESS-TIMESTAMP`` — request time, epoch **milliseconds** (string)
  * ``KALSHI-ACCESS-SIGNATURE`` — base64(RSA-PSS-SHA256 sign of the message)

where the signed message is ``f"{timestamp_ms}{METHOD}{path}"`` and ``path``
*includes* the ``/trade-api/v2`` prefix but *excludes* any query string, e.g.
``"1700000000000GET/trade-api/v2/portfolio/positions"``. The PSS padding uses
MGF1(SHA256) with salt length == digest length.

Source / verified 2026-05-30 against the official Kalshi docs:
  * Signing & headers: https://docs.kalshi.com/getting_started/api_keys.md
  * Environments:       https://docs.kalshi.com/getting_started/api_environments.md
  * CreateOrder:        https://docs.kalshi.com/api-reference/orders/create-order.md
  * CancelOrder:        https://docs.kalshi.com/api-reference/orders/cancel-order.md
  * GetPositions:       https://docs.kalshi.com/api-reference/portfolio/get-positions.md

Cadence: this is a request/response trading API — orders fire on demand; there
is no polling loop here.
"""

from __future__ import annotations

import base64
import math
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from src.execution.adapters.base import BaseAdapter
from src.execution.order import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Outcome,
    TimeInForce,
)

# Optional crypto dependency — guarded so the module always imports cleanly,
# even on a box where ``cryptography`` is not installed. ``is_available()``
# reports False instead of the import blowing up at module load.
try:  # pragma: no cover - import guard
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

    _CRYPTO_OK = True
except Exception:  # pragma: no cover - import guard
    hashes = serialization = padding = rsa = None  # type: ignore[assignment]
    PrivateKeyTypes = Any  # type: ignore[assignment,misc]
    _CRYPTO_OK = False


_HOST_PAPER = "https://demo-api.kalshi.co/trade-api/v2"
_HOST_LIVE = "https://api.elections.kalshi.com/trade-api/v2"
_BASE_PATH = "/trade-api/v2"
_HTTP_TIMEOUT = 10.0

# Kalshi time-in-force enum values (CreateOrder request).
_TIF_MAP = {
    TimeInForce.GTC: "good_till_canceled",
    TimeInForce.GTD: "good_till_canceled",  # GTD handled via expiration_ts below
    TimeInForce.IOC: "immediate_or_cancel",
    TimeInForce.FOK: "fill_or_kill",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    """Parse a Kalshi numeric field that may arrive as int, str, or null."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class KalshiAdapter(BaseAdapter):
    """Execution adapter for Kalshi (binary event contracts)."""

    name = "kalshi"
    asset_classes = ("prediction",)
    supports_modes = ("paper", "live")

    def __init__(self, config: Optional[Any] = None) -> None:
        if config is None:
            # Imported lazily so the module never hard-depends on config at import.
            from src.config import get_config

            config = get_config()
        self._config = config
        self._key_id: str = getattr(config, "kalshi_api_key_id", "") or ""
        self._pem_path: str = getattr(config, "kalshi_private_key_pem_path", "") or ""
        # Cache the parsed private key after first successful load.
        self._private_key: Optional[Any] = None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True when crypto is importable, a key id is set, and the PEM exists."""
        if not _CRYPTO_OK:
            return False
        if not self._key_id:
            return False
        if not self._pem_path:
            return False
        try:
            import os

            return os.path.isfile(self._pem_path)
        except Exception:
            return False

    @property
    def _host(self) -> str:
        """Demo host for PAPER (fake money), elections host for LIVE."""
        return _HOST_LIVE if self._is_live() else _HOST_PAPER

    def _is_live(self) -> bool:
        """LIVE when config asks for live mode (and prod env)."""
        cfg = self._config
        try:
            if hasattr(cfg, "is_live") and cfg.is_live():
                return True
        except Exception:
            pass
        # Fall back to the kalshi_env flag for adapters constructed standalone.
        return getattr(cfg, "kalshi_env", "demo") == "prod"

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def _load_private_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key
        if not _CRYPTO_OK:
            raise RuntimeError("cryptography is not installed; cannot sign requests")
        with open(self._pem_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        self._private_key = key
        return key

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS(SHA256) sign of ``timestamp + METHOD + path`` -> base64.

        ``path`` must include ``/trade-api/v2`` and exclude the query string.
        """
        key = self._load_private_key()
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign(timestamp_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Issue a signed request. ``endpoint`` is the path *after* the v2 base."""
        # The signed path always uses the base path; query string is excluded.
        sign_path = f"{_BASE_PATH}{endpoint}"
        url = f"{self._host}{endpoint}"
        headers = self._auth_headers(method, sign_path)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.request(
                method, url, headers=headers, json=json, params=params
            )
            resp.raise_for_status()
            if not resp.content:
                return {}
            try:
                return resp.json()
            except Exception:
                return {}

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    @staticmethod
    def _taker_fee_dollars(count: int, price: float) -> float:
        """Kalshi general taker fee = ceil(0.07 * count * p * (1 - p)) cents.

        ``price`` is the contract probability in [0, 1]. Returned in dollars.
        """
        if count <= 0:
            return 0.0
        p = max(0.0, min(1.0, price))
        # Kalshi's published general taker fee = ceil(0.07 * C * P * (1 - P)),
        # where P is the price in dollars and the result is dollars of fee,
        # rounded UP to the next whole cent.
        fee_dollars = math.ceil(0.07 * count * p * (1.0 - p) * 100.0) / 100.0
        return fee_dollars

    def _build_body(self, order: Order) -> Dict[str, Any]:
        count = int(round(order.qty))
        if count < 1:
            count = 1
        is_yes = order.outcome == Outcome.YES
        body: Dict[str, Any] = {
            "ticker": order.symbol,
            "action": order.side.value,  # "buy" or "sell"
            "side": "yes" if is_yes else "no",
            "count": count,
            "client_order_id": order.client_order_id or str(uuid.uuid4()),
        }

        if order.order_type == OrderType.LIMIT:
            body["type"] = "limit"
            if order.limit_price is None:
                raise ValueError("limit order requires limit_price")
            price_cents = int(round(float(order.limit_price) * 100))
            price_cents = max(1, min(99, price_cents))
            if is_yes:
                body["yes_price"] = price_cents
            else:
                body["no_price"] = price_cents
            # Time-in-force only meaningful for resting (limit) orders.
            tif = _TIF_MAP.get(order.time_in_force, "good_till_canceled")
            body["time_in_force"] = tif
            if order.time_in_force == TimeInForce.GTD:
                exp = (order.metadata or {}).get("expiration_ts")
                if exp:
                    body["expiration_ts"] = int(exp)
        else:
            body["type"] = "market"
            # Market orders must be immediate-or-cancel on Kalshi.
            body["time_in_force"] = "immediate_or_cancel"

        return body

    async def submit(self, order: Order) -> Order:
        order.broker = self.name

        if not self.is_available():
            order.status = OrderStatus.REJECTED
            order.error = "kalshi adapter not available (missing key id / PEM / cryptography)"
            return order

        # Hard live gate: never fire real-money orders unless config confirms live.
        if self._is_live():
            cfg = self._config
            is_live_ok = bool(getattr(cfg, "is_live", lambda: False)())
            if not is_live_ok:
                order.status = OrderStatus.REJECTED
                order.error = "refusing live submission: config.is_live() is False"
                return order

        try:
            body = self._build_body(order)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"could not build order: {exc}"
            return order

        try:
            data = await self._request("POST", "/portfolio/orders", json=body)
        except httpx.HTTPStatusError as exc:
            order.status = OrderStatus.REJECTED
            detail = ""
            try:
                detail = exc.response.text
            except Exception:
                detail = str(exc)
            order.error = f"kalshi create-order HTTP {exc.response.status_code}: {detail}"
            return order
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"kalshi create-order failed: {exc}"
            return order

        self._apply_order_response(order, data)
        return order

    def _apply_order_response(self, order: Order, data: Dict[str, Any]) -> None:
        """Map a CreateOrder response onto ``order`` (id, status, fills/fees)."""
        ok = data.get("order") if isinstance(data, dict) else None
        ok = ok if isinstance(ok, dict) else {}

        order.broker_order_id = (
            ok.get("order_id") or ok.get("id") or order.broker_order_id
        )

        # ---- Filled count: prefer fixed-point string, fall back to int cents/count.
        filled = None
        if ok.get("fill_count_fp") is not None:
            filled = _to_float(ok.get("fill_count_fp"))
        elif ok.get("taker_fill_count") is not None:
            filled = _to_float(ok.get("taker_fill_count"))
        elif ok.get("fill_count") is not None:
            filled = _to_float(ok.get("fill_count"))
        filled = filled or 0.0

        # ---- Fill price: cost / count, in dollars (probability).
        fill_price = self._infer_fill_price(order, ok, filled)

        # ---- Taker fee in dollars: prefer reported, else compute the formula.
        fee = None
        if ok.get("taker_fees_dollars") is not None:
            fee = _to_float(ok.get("taker_fees_dollars"))
        elif ok.get("taker_fees") is not None:  # cents
            fee = _to_float(ok.get("taker_fees")) / 100.0
        elif ok.get("taker_fee") is not None:  # cents (legacy)
            fee = _to_float(ok.get("taker_fee")) / 100.0
        if fee is None:
            fee = self._taker_fee_dollars(int(round(filled)), fill_price)

        if filled > 0:
            order.add_fill(
                Fill(
                    order_id=order.broker_order_id or order.client_order_id,
                    price=fill_price,
                    qty=filled,
                    fee=fee,
                    venue=self.name,
                    outcome=order.outcome,
                )
            )

        # ---- Status mapping. Kalshi statuses: resting | canceled | executed.
        status = (ok.get("status") or "").lower()
        if status == "executed":
            order.status = OrderStatus.FILLED
        elif status == "canceled":
            order.status = OrderStatus.CANCELLED if filled <= 0 else OrderStatus.PARTIALLY_FILLED
        elif status == "resting":
            order.status = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED
        else:
            # No explicit status — infer from fills (add_fill already set FILLED/PARTIAL).
            if filled <= 0:
                order.status = OrderStatus.SUBMITTED

    def _infer_fill_price(self, order: Order, ok: Dict[str, Any], filled: float) -> float:
        """Derive the average fill price (probability in [0,1]) from the response."""
        if filled <= 0:
            # No fill — fall back to the requested price for bookkeeping.
            return float(order.limit_price) if order.limit_price is not None else 0.0

        # Preferred: total taker cost (dollars) / filled count.
        cost = None
        if ok.get("taker_fill_cost_dollars") is not None:
            cost = _to_float(ok.get("taker_fill_cost_dollars"))
        elif ok.get("taker_fill_cost") is not None:  # cents
            cost = _to_float(ok.get("taker_fill_cost")) / 100.0
        if cost is not None and filled > 0 and cost > 0:
            return max(0.0, min(1.0, cost / filled))

        # Else: the price the order rested/executed at, expressed per outcome.
        is_yes = order.outcome == Outcome.YES
        if is_yes:
            if ok.get("yes_price_dollars") is not None:
                return _to_float(ok.get("yes_price_dollars"))
            if ok.get("yes_price") is not None:  # cents
                return _to_float(ok.get("yes_price")) / 100.0
        else:
            if ok.get("no_price_dollars") is not None:
                return _to_float(ok.get("no_price_dollars"))
            if ok.get("no_price") is not None:  # cents
                return _to_float(ok.get("no_price")) / 100.0

        # Last resort: the limit we asked for.
        return float(order.limit_price) if order.limit_price is not None else 0.0

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        if not broker_order_id:
            return {"ok": False, "error": "no broker_order_id"}
        if not self.is_available():
            return {"ok": False, "error": "kalshi adapter not available"}
        try:
            data = await self._request(
                "DELETE", f"/portfolio/orders/{broker_order_id}"
            )
            return {"ok": True, "order_id": broker_order_id, "response": data}
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "order_id": broker_order_id,
                "status": exc.response.status_code,
                "error": _safe_text(exc),
            }
        except Exception as exc:
            return {"ok": False, "order_id": broker_order_id, "error": str(exc)}

    async def cancel_all(self) -> List[Dict[str, Any]]:
        """Cancel every resting order (best-effort, one DELETE per order)."""
        if not self.is_available():
            return [{"ok": False, "error": "kalshi adapter not available"}]
        try:
            data = await self._request(
                "GET", "/portfolio/orders", params={"status": "resting", "limit": 1000}
            )
        except Exception as exc:
            return [{"ok": False, "error": f"could not list resting orders: {exc}"}]

        orders = data.get("orders") if isinstance(data, dict) else None
        results: List[Dict[str, Any]] = []
        for o in orders or []:
            oid = o.get("order_id") or o.get("id")
            if oid:
                results.append(await self.cancel(oid))
        return results or [{"ok": True, "note": "no resting orders"}]

    # ------------------------------------------------------------------
    # Positions / flatten
    # ------------------------------------------------------------------

    async def get_positions(self) -> List[Dict[str, Any]]:
        if not self.is_available():
            return []
        data = await self._request(
            "GET", "/portfolio/positions", params={"count_filter": "position"}
        )
        market_positions = (
            data.get("market_positions") if isinstance(data, dict) else None
        ) or []

        out: List[Dict[str, Any]] = []
        for mp in market_positions:
            ticker = mp.get("ticker")
            if not ticker:
                continue
            # position is signed: positive = YES contracts, negative = NO.
            if mp.get("position_fp") is not None:
                signed_qty = _to_float(mp.get("position_fp"))
            else:
                signed_qty = _to_float(mp.get("position"))
            if abs(signed_qty) < 1e-9:
                continue
            outcome = Outcome.YES.value if signed_qty > 0 else Outcome.NO.value
            qty = abs(signed_qty)

            # avg_price (probability) = market_exposure / qty.
            exposure = None
            if mp.get("market_exposure_dollars") is not None:
                exposure = _to_float(mp.get("market_exposure_dollars"))
            elif mp.get("market_exposure") is not None:  # cents
                exposure = _to_float(mp.get("market_exposure")) / 100.0
            avg_price = (exposure / qty) if (exposure and qty) else 0.0

            out.append(
                {
                    "symbol": ticker,
                    "qty": qty,
                    "avg_price": round(max(0.0, min(1.0, avg_price)), 6),
                    "outcome": outcome,
                    "event_ticker": mp.get("event_ticker"),
                }
            )
        return out

    async def flatten(self) -> List[Dict[str, Any]]:
        """Cancel resting orders, then market-sell every open position to close."""
        results: List[Dict[str, Any]] = []
        results.extend(await self.cancel_all())

        if not self.is_available():
            return results

        try:
            positions = await self.get_positions()
        except Exception as exc:
            results.append({"ok": False, "error": f"could not read positions: {exc}"})
            return results

        for pos in positions:
            qty = int(round(pos.get("qty", 0)))
            if qty < 1:
                continue
            # To close: sell the same side we hold.
            body = {
                "ticker": pos["symbol"],
                "action": "sell",
                "side": pos.get("outcome", "yes"),
                "count": qty,
                "type": "market",
                "time_in_force": "immediate_or_cancel",
                "client_order_id": f"flatten-{uuid.uuid4()}",
            }
            try:
                data = await self._request("POST", "/portfolio/orders", json=body)
                results.append({"ok": True, "symbol": pos["symbol"], "response": data})
            except Exception as exc:
                results.append({"ok": False, "symbol": pos["symbol"], "error": str(exc)})
        return results


def _safe_text(exc: "httpx.HTTPStatusError") -> str:
    try:
        return exc.response.text
    except Exception:
        return str(exc)
