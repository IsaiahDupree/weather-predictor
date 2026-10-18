"""
PolymarketAdapter — live/paper execution against the Polymarket CLOB.

GATED OFF BY DEFAULT. Polymarket is blocked to US persons; this adapter stays
disabled unless the operator both opts in (``WP_ENABLE_POLYMARKET``) and asserts
non-US-person status (``WP_POLYMARKET_LEGAL_ACK=NON_US_PERSON`` for LIVE).
``is_available()`` enforces ``config.polymarket_allowed()`` plus a usable signer
and an importable SDK, so the adapter never fires on an unsupported config.

Source / cadence
----------------
  * CLOB (Central Limit Order Book) REST API — host ``https://clob.polymarket.com``.
    Verified against https://docs.polymarket.com/developers/CLOB/ and the
    py-clob-client README (https://github.com/Polymarket/py-clob-client).
    Orders are signed (EIP-712) locally and posted on demand — there is no poll
    loop here; the strategy layer drives ``submit()`` per decision.
  * Positions are read best-effort from the public Data API
    ``https://data-api.polymarket.com/positions?user=<address>`` (no auth).

SDK
---
The signing/order-building is done by the official ``py-clob-client`` package
(``pip install py-clob-client``; import ``py_clob_client``). The import is
guarded: if the SDK (or its heavy ``web3``/``eth-account`` deps) is absent,
``is_available()`` returns False with a clear ``.reason`` and the module still
imports cleanly. The SDK is synchronous, so blocking calls are off-loaded to a
thread via ``asyncio.to_thread`` to keep the event loop responsive.

Client construction (verified):
    ClobClient(host, key=PRIVATE_KEY, chain_id=137, signature_type=N, funder=ADDR)
    client.set_api_creds(client.create_or_derive_api_creds())   # derive L2 creds
    order = OrderArgs(token_id=..., price=..., size=..., side=BUY)
    signed = client.create_order(order)
    resp = client.post_order(signed, OrderType.GTC)             # {orderID, success, status, errorMsg}

V2 currency / contract caveat
-----------------------------
Polymarket's newer "V2" markets settle in **pUSD** (a wrapped/positional USDC
collateral) rather than the legacy USDC.e (bridged USDC) used by the original
binary CTF markets. The collateral token, exchange contract address, and the
``signature_type``/``funder`` plumbing differ between the two; an account funded
for one is not automatically usable on the other. We do NOT auto-bridge or
auto-approve collateral here — the operator is responsible for funding the
correct collateral and granting the necessary token allowances out of band.
``signature_type`` semantics (per docs): 0=EOA, 1=POLY_PROXY (Magic/email),
2=GNOSIS_SAFE, 3=POLY_1271. Config default is 1.

Outcome mapping
---------------
Polymarket has no native "sell-to-open NO"; the NO view is a BUY of the NO
token. ``order.symbol`` is the YES ``clobTokenId``; ``order.metadata['token_id_no']``
is the NO ``clobTokenId``. We pick the token by ``order.outcome`` and always
submit ``side=BUY`` to the venue.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from src.config import Config, get_config
from src.execution.adapters.base import BaseAdapter
from src.execution.order import Fill, Order, OrderStatus, Outcome, Side

# --- Optional SDK import (must never raise at module import time) ----------
_SDK_OK = False
_SDK_IMPORT_ERR: Optional[str] = None
try:  # pragma: no cover - depends on optional dependency being installed
    from py_clob_client.client import ClobClient  # type: ignore
    from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
    from py_clob_client.order_builder.constants import BUY  # type: ignore

    _SDK_OK = True
except Exception as _exc:  # ImportError or any transitive dep failure
    ClobClient = None  # type: ignore
    OrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    BUY = "BUY"  # type: ignore
    _SDK_IMPORT_ERR = f"{type(_exc).__name__}: {_exc}"


CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137
_DEFAULT_TICK = 0.01  # CLOB tick sizes are 0.1 / 0.01 / 0.001 / 0.0001
_HTTP_TIMEOUT = 10.0


def _snap_to_tick(price: float, tick: float = _DEFAULT_TICK) -> float:
    """Round a probability price to the venue tick and clamp into (0, 1)."""
    if tick <= 0:
        tick = _DEFAULT_TICK
    snapped = round(round(price / tick) * tick, 6)
    # Keep strictly inside the tradable band so it is never 0.0 or 1.0.
    lo, hi = tick, round(1.0 - tick, 6)
    return max(lo, min(hi, snapped))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketAdapter(BaseAdapter):
    """Execution adapter for the Polymarket CLOB (paper dry-run + live post)."""

    name = "polymarket"
    asset_classes = ("prediction",)
    supports_modes = ("paper", "live")

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config: Config = config or get_config()
        self.reason: Optional[str] = None
        self._client: Optional[Any] = None        # lazily-built ClobClient
        self._client_err: Optional[str] = None
        self._tick_cache: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True only when PM is legally enabled, a signer key exists, and the SDK loaded."""
        cfg = self._config
        if not cfg.polymarket_allowed():
            self.reason = (
                "polymarket gated off (set WP_ENABLE_POLYMARKET=1 and, for LIVE, "
                "WP_POLYMARKET_LEGAL_ACK=NON_US_PERSON)"
            )
            return False
        if not cfg.polymarket_pk:
            self.reason = "no POLYMARKET_PK (L1 signer private key) configured"
            return False
        if not _SDK_OK:
            self.reason = f"py-clob-client SDK not importable ({_SDK_IMPORT_ERR})"
            return False
        self.reason = None
        return True

    # ------------------------------------------------------------------
    # Lazy client construction (synchronous SDK)
    # ------------------------------------------------------------------

    def _build_client(self) -> Any:
        """Construct + authenticate the ClobClient once. Raises on failure."""
        if self._client is not None:
            return self._client
        cfg = self._config
        client = ClobClient(  # type: ignore[call-arg]
            CLOB_HOST,
            key=cfg.polymarket_pk,
            chain_id=cfg.polymarket_chain_id or POLYGON_CHAIN_ID,
            signature_type=cfg.polymarket_signature_type,
            funder=cfg.polymarket_funder or None,
        )
        # Derive L2 API credentials from the L1 signer and attach them.
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        self._client = client
        return client

    async def _client_async(self) -> Any:
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        return self._client

    def _tick_size(self, client: Any, token_id: str) -> float:
        """Best-effort venue tick size for a token (falls back to 0.01)."""
        if token_id in self._tick_cache:
            return self._tick_cache[token_id]
        tick = _DEFAULT_TICK
        try:
            raw = client.get_tick_size(token_id)
            tick = _to_float(raw, _DEFAULT_TICK) or _DEFAULT_TICK
        except Exception:
            tick = _DEFAULT_TICK
        self._tick_cache[token_id] = tick
        return tick

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(self, order: Order) -> Order:
        """Sign a CLOB order. PAPER: sign-only dry-run. LIVE: post to the book."""
        order.broker = self.name

        if not self.is_available():
            order.status = OrderStatus.REJECTED
            order.error = self.reason or "polymarket adapter unavailable"
            return order

        if order.mode.value not in self.supports_modes:
            order.status = OrderStatus.REJECTED
            order.error = f"polymarket adapter does not support mode={order.mode.value}"
            return order

        # Resolve which token to trade (YES = symbol, NO = metadata token_id_no).
        if order.outcome == Outcome.NO:
            token_id = (order.metadata or {}).get("token_id_no")
            if not token_id:
                order.status = OrderStatus.REJECTED
                order.error = "NO order missing metadata['token_id_no']"
                return order
        else:
            token_id = order.symbol
        token_id = str(token_id)

        if order.limit_price is None:
            order.status = OrderStatus.REJECTED
            order.error = "polymarket requires a limit_price (probability in [0,1])"
            return order

        try:
            client = await self._client_async()
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"clob client init failed: {exc}"
            return order

        try:
            return await asyncio.to_thread(self._submit_blocking, client, order, token_id)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"polymarket submit failed: {exc}"
            return order

    def _submit_blocking(self, client: Any, order: Order, token_id: str) -> Order:
        """Runs in a worker thread — all blocking SDK calls live here."""
        tick = self._tick_size(client, token_id)
        price = _snap_to_tick(float(order.limit_price), tick)
        size = float(order.qty)

        # Build + sign the EIP-712 CLOB order. Polymarket always BUYs the chosen
        # outcome token (the NO view is a BUY of the NO token), so side=BUY.
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)  # type: ignore[call-arg]
        signed = client.create_order(args)

        order.metadata = dict(order.metadata or {})
        order.metadata["token_id"] = token_id
        order.metadata["snapped_price"] = price

        cfg = self._config

        # PAPER: build + sign but never post — true dry-run.
        if order.mode.value == "paper":
            order.status = OrderStatus.SUBMITTED
            order.error = None
            order.metadata["dry_run"] = True
            return order

        # LIVE: hard double-gate before any real post.
        if not (cfg.is_live() and cfg.polymarket_allowed()):
            order.status = OrderStatus.REJECTED
            order.error = "live post refused: config is not LIVE or polymarket not allowed"
            return order

        order_type = self._order_type_for(order)
        resp = client.post_order(signed, order_type)
        return self._apply_response(order, resp, price)

    @staticmethod
    def _order_type_for(order: Order) -> Any:
        """Map our TimeInForce to py-clob-client OrderType (GTC/GTD/FOK/FAK)."""
        tif = order.time_in_force.value  # gtc | gtd | ioc | fok
        mapping = {
            "gtc": getattr(OrderType, "GTC", None),
            "gtd": getattr(OrderType, "GTD", None),
            "fok": getattr(OrderType, "FOK", None),
            "ioc": getattr(OrderType, "FAK", None),  # IOC == fill-and-kill on PM
        }
        return mapping.get(tif) or getattr(OrderType, "GTC", None)

    def _apply_response(self, order: Order, resp: Any, price: float) -> Order:
        """Parse a post_order response into the unified Order."""
        data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {}) or {}
        success = bool(data.get("success", False))
        broker_id = data.get("orderID") or data.get("orderId") or data.get("id")
        status = str(data.get("status", "") or "").lower()
        err = data.get("errorMsg") or data.get("error") or None

        if broker_id:
            order.broker_order_id = str(broker_id)

        if not success and err:
            order.status = OrderStatus.REJECTED
            order.error = str(err)
            return order

        # status: live | matched | delayed | unmatched
        if status == "matched":
            order.add_fill(
                Fill(
                    order_id=order.broker_order_id or "",
                    price=price,
                    qty=float(order.qty),
                    venue=self.name,
                    outcome=order.outcome,
                )
            )
            # add_fill sets FILLED/PARTIALLY_FILLED; nothing more to do.
        elif status in ("live", "delayed", "unmatched", ""):
            order.status = OrderStatus.SUBMITTED
        else:
            order.status = OrderStatus.SUBMITTED
        order.error = None
        order.metadata = dict(order.metadata or {})
        order.metadata["venue_status"] = status
        return order

    # ------------------------------------------------------------------
    # Cancel / flatten
    # ------------------------------------------------------------------

    async def cancel(self, broker_order_id: str) -> Dict[str, Any]:
        if not self.is_available():
            return {"ok": False, "error": self.reason or "unavailable"}
        try:
            client = await self._client_async()
            resp = await asyncio.to_thread(client.cancel, broker_order_id)
            return {"ok": True, "order_id": broker_order_id, "response": resp}
        except Exception as exc:
            return {"ok": False, "order_id": broker_order_id, "error": str(exc)}

    async def cancel_all(self) -> List[Dict[str, Any]]:
        if not self.is_available():
            return [{"ok": False, "error": self.reason or "unavailable"}]
        try:
            client = await self._client_async()
            resp = await asyncio.to_thread(client.cancel_all)
            return [{"ok": True, "response": resp}]
        except Exception as exc:
            return [{"ok": False, "error": str(exc)}]

    async def flatten(self) -> List[Dict[str, Any]]:
        """Polymarket has no one-shot flatten; cancel resting orders as a best effort.

        Open positions resolve to $1/$0 on settlement and cannot be 'closed' by a
        venue call — exiting early means submitting opposing BUY orders, which the
        strategy layer owns. Here we just pull resting orders off the book.
        """
        return await self.cancel_all()

    # ------------------------------------------------------------------
    # Positions (public Data API — no auth)
    # ------------------------------------------------------------------

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Best-effort open positions for the funder/signer address.

        Reads the public Data API ``/positions?user=<address>``. Returns [] when
        the address is unknown or the call fails — never raises.
        """
        if not self.is_available():
            return []
        cfg = self._config
        address = cfg.polymarket_funder
        if not address:
            # Derive the signer's address from the SDK if available.
            try:
                client = await self._client_async()
                address = await asyncio.to_thread(getattr(client, "get_address"))
            except Exception:
                address = ""
        if not address:
            return []

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
                resp = await http.get(
                    f"{DATA_API_HOST}/positions",
                    params={"user": address, "sizeThreshold": "0"},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                rows = resp.json()
        except Exception:
            return []

        if not isinstance(rows, list):
            return []

        positions: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = _to_float(row.get("size"))
            if abs(qty) <= 1e-9:
                continue
            outcome_raw = str(row.get("outcome", "") or "").lower()
            outcome = Outcome.NO.value if outcome_raw == "no" else Outcome.YES.value
            positions.append(
                {
                    "symbol": str(row.get("asset", "") or ""),
                    "qty": qty,
                    "avg_price": _to_float(row.get("avgPrice")),
                    "outcome": outcome,
                    "event_ticker": row.get("conditionId") or row.get("eventSlug"),
                }
            )
        return positions
