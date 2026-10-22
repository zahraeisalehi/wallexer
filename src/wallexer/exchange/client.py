"""Async Wallex REST client.

Scope is deliberately closed: only the endpoints in the Market data / Account / Orders tables of
docs/wallex-api.md exist here. No withdrawal, transfer, IBAN, card, wallet or OTC endpoints.

Safety:
- Private requests always carry the trading-credit sub-account header.
- GETs are retried with backoff; POST/DELETE never are (a retry could double an order).
- Order placement is idempotent via our client_id: on timeout we look the order up, never re-send.
- Orders are latched off (`orders_enabled=False`) unless the live executor enables them.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from wallexer.config import Settings
from wallexer.exchange import min_order
from wallexer.exchange.errors import (
    ApiError,
    OrderRejected,
    OrdersDisabled,
    OrderStatusUnknown,
    TransportError,
)
from wallexer.exchange.fees import FeeTable, parse_fee_table
from wallexer.exchange.models import Balance, Book, Candles, Market, Order, Trade
from wallexer.exchange.ratelimit import EventSink, MarketDataLimiter, OrderLimiter
from wallexer.exchange.rules import OrderSpec

log = structlog.get_logger(__name__)

SUBACCOUNT_HEADER = "sub-account-client-id"
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _noop(kind: str, payload: dict[str, Any]) -> None:
    return None


def parse_json(text: str) -> Any:
    # parse_float=Decimal: depth levels arrive as JSON floats; never let them become binary floats.
    return json.loads(text, parse_float=Decimal)


class WallexClient:
    def __init__(
        self,
        settings: Settings,
        *,
        on_event: EventSink = _noop,
        orders_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        ex = settings.cfg.exchange
        rl = settings.cfg.ratelimit
        self.on_event = on_event
        self._orders_enabled = orders_enabled
        self.market_limiter = MarketDataLimiter(rl.market.window_s, rl.market.backoff_below_frac)
        self.order_limiter = OrderLimiter(
            rl.orders.max_requests, rl.orders.window_s, rl.orders.backlog_warn, on_event=on_event
        )
        self._http = httpx.AsyncClient(
            base_url=ex.base_url,
            timeout=ex.timeout_s,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "wallexer/0.1"},
        )
        self._rng = random.Random(0)  # jitter only; seeded for reproducibility
        self.last_headers: httpx.Headers | None = None
        self.last_server_time: float | None = None
        self.last_local_time: float | None = None
        self._fee_shape_logged = False

    async def __aenter__(self) -> WallexClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ plumbing

    def _private_headers(self) -> dict[str, str]:
        key, sub = self.settings.require_private()
        return {"x-api-key": key, SUBACCOUNT_HEADER: sub}

    def _record(self, resp: httpx.Response) -> None:
        self.last_headers = resp.headers
        self.market_limiter.update(resp.headers)
        date = resp.headers.get("date")
        # Cached responses (e.g. /markets, x-cache-status: HIT, max-age=30) carry a stale Date.
        if date and resp.headers.get("x-cache-status", "").upper() != "HIT":
            try:
                self.last_server_time = parsedate_to_datetime(date).timestamp()
                self.last_local_time = time.time()
            except (TypeError, ValueError):
                pass

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        try:
            body = parse_json(resp.text)
        except ValueError as e:
            snippet = resp.text[:80].replace("\n", " ")
            raise TransportError(f"HTTP {resp.status_code}: non-JSON body: {snippet!r}") from e
        if resp.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
            msg = body.get("message", "") if isinstance(body, dict) else str(body)
            code = body.get("code") if isinstance(body, dict) else None
            fields = body.get("result") if isinstance(body, dict) else None
            raise ApiError(
                resp.status_code, str(msg), code, fields if isinstance(fields, dict) else {}
            )
        return body

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        timeout: float | None = None,
    ) -> httpx.Response:
        headers = self._private_headers() if private else {}
        await self.market_limiter.acquire()
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.request(method, path, **kwargs)
        self._record(resp)
        return resp

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None, private: bool = False
    ) -> Any:
        ex = self.settings.cfg.exchange
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last_err: Exception | None = None
        for attempt in range(ex.get_retries + 1):
            try:
                resp = await self._send("GET", path, params=params, private=private)
                if resp.status_code in _RETRY_STATUS:
                    last_err = TransportError(f"HTTP {resp.status_code} on GET {path}")
                else:
                    return self._decode(resp)
            except httpx.TransportError as e:
                last_err = TransportError(f"GET {path}: {type(e).__name__}: {e}")
            if attempt < ex.get_retries:
                delay = ex.backoff_base_s * (2**attempt) * (1 + self._rng.random() * 0.25)
                log.info("http.retry", path=path, attempt=attempt + 1, sleep_s=round(delay, 2))
                await asyncio.sleep(delay)
        assert last_err is not None
        raise last_err

    # ------------------------------------------------------------------ market data (public)

    async def markets(self) -> list[Market]:
        body = await self._get("/hector/web/v1/markets")
        out: list[Market] = []
        for raw in body["result"]["markets"]:
            try:
                out.append(Market.model_validate(raw))
            except ValidationError as e:
                # One malformed market must not take down the whole universe; it is skipped.
                log.warning("markets.skip_invalid", symbol=raw.get("symbol"), error=str(e)[:120])
        return out

    async def depth_all(self) -> dict[str, Book]:
        # Sent with the key when available: unauthenticated calls returned nginx 503 in testing.
        body = await self._get("/depth/all", private=self.settings.has_private)
        return {sym: Book.model_validate(b) for sym, b in (body.get("result") or {}).items() if b}

    async def depth(self, symbol: str) -> Book:
        body = await self._get("/v1/depth", params={"symbol": symbol})
        return Book.model_validate(body["result"])

    async def trades(self, symbol: str) -> list[Trade]:
        body = await self._get("/v1/trades", params={"symbol": symbol})
        return [Trade.model_validate(t) for t in body["result"]["latestTrades"]]

    async def udf_history(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> Candles:
        # No {success,result} envelope on this endpoint.
        body = await self._get(
            "/v1/udf/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts},
        )
        return Candles.model_validate(body)

    # ------------------------------------------------------------------ account (private)

    async def balances(self) -> dict[str, Balance]:
        body = await self._get("/v1/account/balances", private=True)
        raw = body["result"]["balances"]
        return {k: Balance.model_validate(v) for k, v in raw.items()}

    async def fees(self) -> FeeTable:
        """Fee table; markets without usable data fall back to the config default."""
        body = await self._get("/v1/account/fee", private=True)
        result = body.get("result") if isinstance(body, dict) else None
        fcfg = self.settings.cfg.fees
        table = parse_fee_table(result, fcfg.default_maker, fcfg.default_taker)
        if not self._fee_shape_logged:
            # Once per client: what the account actually returns (structure only, no secrets).
            self._fee_shape_logged = True
            emit = log.info if table.source == "exchange" else log.warning
            emit(
                "fees.raw_shape",
                shape=table.raw_shape,
                usable=len(table.rates),
                skipped=len(table.skipped),
                source=table.source,
            )
        return table

    # ------------------------------------------------------------------ orders (private)

    def enable_orders(self) -> None:
        """Only the live executor (trade mode) may call this."""
        self._orders_enabled = True

    def _check_latch(self) -> None:
        if not self._orders_enabled:
            raise OrdersDisabled("order requests are disabled (not in live trade mode)")

    async def get_order(self, client_id: str) -> Order | None:
        """Single order by our client_id. None if the exchange says 404."""
        try:
            body = await self._get(f"/v1/account/orders/{client_id}", private=True)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return Order.model_validate(body["result"])

    async def place_order(
        self, spec: OrderSpec, *, quote_asset: str, usdt_tmn: Decimal | None = None
    ) -> Order:
        """Submit once. Timeout → look up by client_id; never re-POST automatically."""
        self._check_latch()
        await self.order_limiter.acquire("create")
        log.info(
            "order.submit", client_id=spec.client_id, decision_id=spec.decision_id, **spec.to_body()
        )
        try:
            resp = await self._send(
                "POST",
                "/v1/account/orders",
                body=spec.to_body(),
                private=True,
                timeout=self.settings.cfg.exchange.order_timeout_s,
            )
        except httpx.TransportError as e:
            log.warning("order.submit_uncertain", client_id=spec.client_id, error=str(e))
            return await self._resolve_uncertain(spec.client_id)
        try:
            body = self._decode(resp)
        except ApiError as e:
            if e.status in (400, 422):
                min_order.handle_rejection(
                    self.settings, e, quote_asset, usdt_tmn, on_event=self.on_event
                )
                raise OrderRejected(e.status, e.message, e.code, e.fields) from e
            raise
        return Order.model_validate(body["result"])

    async def _resolve_uncertain(self, client_id: str) -> Order:
        for delay in (1.0, 3.0):
            await asyncio.sleep(delay)
            try:
                order = await self.get_order(client_id)
            except (ApiError, TransportError):
                continue
            if order is not None:
                return order
        self.on_event("risk.order_status_unknown", {"client_id": client_id})
        raise OrderStatusUnknown(client_id)

    async def cancel_order(self, client_id: str) -> Order:
        """DELETE /v1/account/orders/{client_id}. Counted by the order limiter."""
        self._check_latch()
        await self.order_limiter.acquire("cancel")
        resp = await self._send("DELETE", f"/v1/account/orders/{client_id}", private=True)
        return Order.model_validate(self._decode(resp)["result"])

    async def cancel_order_body(self, client_id: str) -> Order:
        """DELETE /v1/account/orders with body {clientOrderId}. Counted by the order limiter."""
        self._check_latch()
        await self.order_limiter.acquire("cancel")
        resp = await self._send(
            "DELETE", "/v1/account/orders", body={"clientOrderId": client_id}, private=True
        )
        return Order.model_validate(self._decode(resp)["result"])

    async def open_orders(
        self, symbol: str | None = None, page: int | None = None, per_page: int | None = None
    ) -> dict[str, list[Order]]:
        body = await self._get(
            "/v1/account/openOrders",
            params={"symbol": symbol, "page": page, "per_page": per_page},
            private=True,
        )
        result = body.get("result") or {}
        return {s: [Order.model_validate(o) for o in lst] for s, lst in result.items()}

    async def order_history(
        self,
        market: str | None = None,
        type: str | None = None,
        side: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, list[Order]]:
        """Dates are YYYY-MM-DD; per_page max 100."""
        if per_page is not None:
            per_page = min(per_page, 100)
        body = await self._get(
            "/v1/account/orders",
            params={
                "market": market,
                "type": type,
                "side": side,
                "page": page,
                "per_page": per_page,
                "from": from_date,
                "to": to_date,
            },
            private=True,
        )
        result = body.get("result") or {}
        return {s: [Order.model_validate(o) for o in lst] for s, lst in result.items()}
