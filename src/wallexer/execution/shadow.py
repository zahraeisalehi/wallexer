"""ShadowExecutor: live data, simulated account. Starts from the real balance snapshot.

Same fill model as the paper books: orders that cross the book (MARKET, or LIMIT through the
opposite best) fill immediately as taker, walking the book + slippage; resting LIMITs fill as
maker when a trade prints at/through their price. Orders/fills are written with mode='shadow'.
Never touches the exchange order endpoints.
"""

from __future__ import annotations

import sqlite3
import time
from decimal import Decimal

from wallexer.exchange.fees import FeeTable
from wallexer.exchange.models import Book, OrderType, Side, Trade
from wallexer.exchange.rules import OrderSpec
from wallexer.execution.base import AccountSnapshot, ExecResult, OpenOrder
from wallexer.execution.paper import FillModel
from wallexer.store import repo

ZERO = Decimal(0)


class ShadowExecutor:
    mode = "shadow"

    def __init__(
        self,
        conn: sqlite3.Connection,
        fees: FeeTable,
        fill: FillModel,
        start_balances: dict[str, Decimal],
        base_of: dict[str, str],
    ) -> None:
        self.conn = conn
        self.fees = fees
        self.fill = fill
        self.free: dict[str, Decimal] = dict(start_balances)
        self.locked: dict[str, Decimal] = {}
        self.base_of = base_of  # symbol -> base asset
        self.open_orders: dict[str, OpenOrder] = {}
        self.reserved: dict[str, Decimal] = {}  # client_id -> amount locked at submit

    def _apply_fill(self, o: OpenOrder, px: Decimal, maker: bool) -> ExecResult:
        spec = o.spec
        fee_rate = self.fees.get(spec.symbol).maker if maker else self.fees.get(spec.symbol).taker
        base = self.base_of.get(spec.symbol, spec.symbol[: -len(o.quote)])
        qty = spec.quantity
        value = qty * px
        reserved = self.reserved.pop(spec.client_id, ZERO)
        if spec.side is Side.BUY:
            # release the reservation, pay the actual cost (a MARKET buy may cost more than
            # reserved at the ask; the difference comes out of free)
            self.locked[o.quote] = self.locked.get(o.quote, ZERO) - reserved
            self.free[o.quote] = self.free.get(o.quote, ZERO) + reserved - value
            fee = qty * fee_rate  # fee in the received asset
            self.free[base] = self.free.get(base, ZERO) + qty - fee
            fee_quote = fee * px
        else:
            self.locked[base] = self.locked.get(base, ZERO) - qty
            fee_quote = value * fee_rate
            self.free[o.quote] = self.free.get(o.quote, ZERO) + value - fee_quote
        repo.update_order(
            self.conn,
            spec.client_id,
            status="filled",
            executed_qty=qty,
            executed_price=px,
            fee=fee_quote,
        )
        repo.insert_fill(
            self.conn,
            client_id=spec.client_id,
            price=px,
            quantity=qty,
            fee=fee_quote,
            fee_asset=o.quote,
            is_maker=maker,
        )
        self.open_orders.pop(spec.client_id, None)
        return ExecResult(spec.client_id, "filled", qty, px)

    def _crosses(self, spec: OrderSpec, book: Book | None) -> bool:
        if spec.type is OrderType.MARKET or book is None:
            return spec.type is OrderType.MARKET
        if spec.side is Side.BUY:
            return (
                book.best_ask is not None and spec.price is not None and spec.price >= book.best_ask
            )
        return book.best_bid is not None and spec.price is not None and spec.price <= book.best_bid

    async def submit(
        self, spec: OrderSpec, *, quote: str, book: Book | None, is_exit: bool
    ) -> ExecResult:
        base = self.base_of.get(spec.symbol, spec.symbol[: -len(quote)])
        # reserve funds like the exchange would
        if spec.side is Side.BUY:
            ref = spec.price or (book.best_ask if book and book.best_ask else ZERO)
            need = spec.quantity * ref
            if self.free.get(quote, ZERO) < need:
                return ExecResult(spec.client_id, "rejected", error="insufficient balance (shadow)")
            self.free[quote] -= need
            self.locked[quote] = self.locked.get(quote, ZERO) + need
            self.reserved[spec.client_id] = need
        else:
            if self.free.get(base, ZERO) < spec.quantity:
                return ExecResult(spec.client_id, "rejected", error="insufficient balance (shadow)")
            self.free[base] -= spec.quantity
            self.locked[base] = self.locked.get(base, ZERO) + spec.quantity
        o = OpenOrder(spec, time.time(), quote, is_exit=is_exit)
        self.open_orders[spec.client_id] = o
        if self._crosses(spec, book) and book is not None and book.best_bid and book.best_ask:
            px = self.fill.taker_price(spec.side, spec.quantity, book.best_bid, book.best_ask, book)
            if spec.type is OrderType.LIMIT and spec.price is not None:
                # a LIMIT never fills worse than its price
                px = min(px, spec.price) if spec.side is Side.BUY else max(px, spec.price)
            return self._apply_fill(o, px, maker=False)
        repo.update_order(self.conn, spec.client_id, status="open")
        return ExecResult(spec.client_id, "open")

    async def cancel(self, client_id: str) -> None:
        o = self.open_orders.pop(client_id, None)
        if o is None:
            return
        spec = o.spec
        if spec.side is Side.BUY:
            amt = self.reserved.pop(client_id, ZERO)
            self.locked[o.quote] = self.locked.get(o.quote, ZERO) - amt
            self.free[o.quote] = self.free.get(o.quote, ZERO) + amt
        else:
            base = self.base_of.get(spec.symbol, spec.symbol[: -len(o.quote)])
            self.locked[base] = self.locked.get(base, ZERO) - spec.quantity
            self.free[base] = self.free.get(base, ZERO) + spec.quantity
        repo.update_order(self.conn, client_id, status="cancelled")

    async def on_market(
        self, books: dict[str, Book], trades: dict[str, list[Trade]], now: float
    ) -> list[ExecResult]:
        out = []
        for o in list(self.open_orders.values()):
            spec = o.spec
            if spec.price is None:
                continue
            ts = trades.get(spec.symbol, [])
            if spec.side is Side.BUY:
                hit = any(t.price <= spec.price for t in ts)
            else:
                hit = any(t.price >= spec.price for t in ts)
            book = books.get(spec.symbol)
            if hit:
                out.append(self._apply_fill(o, spec.price, maker=True))
            elif self._crosses(spec, book) and book and book.best_bid and book.best_ask:
                out.append(self._apply_fill(o, spec.price, maker=False))
        return out

    async def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(dict(self.free), {k: v for k, v in self.locked.items() if v})
