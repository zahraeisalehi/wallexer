"""LiveExecutor: real orders. The ONLY place that enables the client's order latch, and only in
trade mode. Every order arrives here already tied to a decision row (orders.decision_id FK).

The exchange is the source of truth: balances and open orders are re-read on every reconcile.
Exits that are aggressive LIMITs (MARKET disabled on the market) are re-priced while unfilled:
cancel -> re-read executedQty -> re-place the remainder at a fresh aggressive price.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from decimal import Decimal

import structlog

from wallexer.config import Mode, Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, OrderRejected, OrderStatusUnknown, TransportError
from wallexer.exchange.models import Book, OrderType, Trade
from wallexer.exchange.rules import MarketRules, OrderRuleError, OrderSpec, reprice_exit
from wallexer.execution.base import AccountSnapshot, ExecResult, OpenOrder
from wallexer.store import repo
from wallexer.store.db import log_event

log = structlog.get_logger(__name__)
ZERO = Decimal(0)


class LiveExecutor:
    mode = "trade"

    def __init__(
        self,
        settings: Settings,
        client: WallexClient,
        conn: sqlite3.Connection,
        rules: dict[str, MarketRules],
        usdt_tmn: Callable[[], Decimal | None],
        mode: Mode,
    ) -> None:
        if mode is not Mode.trade:
            raise RuntimeError("LiveExecutor may only be created in trade mode")
        self.settings = settings
        self.client = client
        self.conn = conn
        self.rules = rules
        self.usdt_tmn = usdt_tmn
        self.open_orders: dict[str, OpenOrder] = {}
        client.enable_orders()  # the single call site of the order latch
        log.warning("live.orders_enabled")

    async def submit(
        self, spec: OrderSpec, *, quote: str, book: Book | None, is_exit: bool
    ) -> ExecResult:
        try:
            order = await self.client.place_order(spec, quote_asset=quote, usdt_tmn=self.usdt_tmn())
        except OrderRejected as e:
            repo.update_order(self.conn, spec.client_id, status="rejected", error=e.message[:300])
            return ExecResult(spec.client_id, "rejected", error=e.message)
        except OrderStatusUnknown as e:
            repo.update_order(self.conn, spec.client_id, status="unknown", error=str(e))
            # Track it: reconcile will find it (or not) by client_id. Never re-send blindly.
            self.open_orders[spec.client_id] = OpenOrder(spec, time.time(), quote, is_exit=is_exit)
            return ExecResult(spec.client_id, "unknown", error=str(e))
        except (ApiError, TransportError) as e:
            repo.update_order(self.conn, spec.client_id, status="error", error=str(e)[:300])
            return ExecResult(spec.client_id, "rejected", error=str(e))
        status = (
            "filled"
            if order.active is False and order.executed_qty >= spec.quantity
            else (order.status or "open")
        )
        repo.update_order(
            self.conn,
            spec.client_id,
            status=status,
            executed_qty=order.executed_qty,
            executed_price=order.executed_price,
            fee=order.fee,
        )
        if order.active is not False:
            self.open_orders[spec.client_id] = OpenOrder(
                spec, time.time(), quote, order.executed_qty, is_exit
            )
        return ExecResult(spec.client_id, status, order.executed_qty, order.executed_price)

    async def cancel(self, client_id: str) -> None:
        try:
            await self.client.cancel_order(client_id)
        except (ApiError, TransportError) as e:
            log.warning("live.cancel_failed", client_id=client_id, error=str(e)[:120])
        o = await self.client.get_order(client_id)
        if o is not None:
            repo.update_order(
                self.conn, client_id, status=o.status or "cancelled", executed_qty=o.executed_qty
            )
            if o.active is False:
                self.open_orders.pop(client_id, None)

    async def on_market(
        self, books: dict[str, Book], trades: dict[str, list[Trade]], now: float
    ) -> list[ExecResult]:
        """Poll tracked orders; re-price unfilled aggressive-LIMIT exits."""
        oc = self.settings.cfg.orders
        out: list[ExecResult] = []
        for cid, o in list(self.open_orders.items()):
            try:
                order = await self.client.get_order(cid)
            except (ApiError, TransportError):
                continue
            if order is None:
                continue
            if order.active is False:
                self.open_orders.pop(cid, None)
                repo.update_order(
                    self.conn,
                    cid,
                    status=order.status or "done",
                    executed_qty=order.executed_qty,
                    executed_price=order.executed_price,
                    fee=order.fee,
                )
                for fl in order.fills:
                    repo.insert_fill(
                        self.conn,
                        client_id=cid,
                        price=fl.price,
                        quantity=fl.quantity,
                        fee=fl.fee or ZERO,
                        fee_asset=fl.fee_asset,
                        is_maker=None,
                    )
                out.append(ExecResult(cid, "filled", order.executed_qty, order.executed_price))
                continue
            if not (o.is_exit and o.spec.type is OrderType.LIMIT):
                continue
            if now - o.ts < oc.exit_reprice_after_s:
                continue
            # re-price: cancel, re-read (fills racing the cancel), re-place the remainder
            await self.cancel(cid)
            after = await self.client.get_order(cid)
            book = books.get(o.spec.symbol)
            rules = self.rules.get(o.spec.symbol)
            if after is None or book is None or rules is None:
                continue
            self.open_orders.pop(cid, None)
            try:
                d = reprice_exit(
                    rules,
                    after,
                    book,
                    decision_id=o.spec.decision_id,
                    attempt=o.reprices + 1,
                    slippage_pct=oc.exit_limit_slippage_pct,
                    max_reprices=oc.exit_max_reprices,
                )
            except OrderRuleError as e:
                log_event(
                    self.conn, "risk.exit_reprice_failed", {"client_id": cid, "error": str(e)}
                )
                continue
            if d.spec is None:
                continue
            if d.exhausted:
                log_event(
                    self.conn, "risk.exit_unfilled", {"client_id": cid, "attempt": o.reprices + 1}
                )
            repo.insert_order(
                self.conn,
                decision_id=int(o.spec.decision_id),
                client_id=d.spec.client_id,
                mode="trade",
                symbol=d.spec.symbol,
                side=d.spec.side.value,
                type_=d.spec.type.value,
                price=d.spec.price,
                quantity=d.spec.quantity,
                status="new",
            )
            res = await self.submit(d.spec, quote=o.quote, book=book, is_exit=True)
            if d.spec.client_id in self.open_orders:
                self.open_orders[d.spec.client_id].reprices = o.reprices + 1
            out.append(res)
        return out

    async def snapshot(self) -> AccountSnapshot:
        bals = await self.client.balances()
        return AccountSnapshot(
            {a: b.value for a, b in bals.items()},
            {a: b.locked for a, b in bals.items() if b.locked},
        )

    async def reconcile_open_orders(self) -> None:
        """Exchange truth: adopt our wx- orders we don't track (e.g. after a restart)."""
        try:
            remote = await self.client.open_orders()
        except (ApiError, TransportError):
            return
        for sym_orders in remote.values():
            for o in sym_orders:
                cid = o.client_order_id
                if cid.startswith("wx-") and cid not in self.open_orders:
                    log_event(
                        self.conn,
                        "reconcile.adopted_open_order",
                        {"client_id": cid, "symbol": o.symbol},
                    )
