"""Executor protocol: the only part that differs between shadow and live."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from wallexer.exchange.models import Book, Trade
from wallexer.exchange.rules import OrderSpec


@dataclass
class OpenOrder:
    spec: OrderSpec
    ts: float
    quote: str
    executed_qty: Decimal = Decimal(0)
    is_exit: bool = False
    reprices: int = 0


@dataclass
class ExecResult:
    client_id: str
    status: str  # filled | open | rejected | unknown
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee_tmn: Decimal = Decimal(0)
    error: str | None = None


@dataclass
class AccountSnapshot:
    free: dict[str, Decimal] = field(default_factory=dict)
    locked: dict[str, Decimal] = field(default_factory=dict)

    # free = available to sell/spend; locked = reserved by open orders; total = free + locked
    # (= the exchange's balance `value`, which already includes locked funds).
    def total(self, asset: str) -> Decimal:
        return self.free.get(asset, Decimal(0)) + self.locked.get(asset, Decimal(0))


class Executor(Protocol):
    mode: str
    open_orders: dict[str, OpenOrder]

    async def submit(
        self, spec: OrderSpec, *, quote: str, book: Book | None, is_exit: bool
    ) -> ExecResult: ...

    async def cancel(self, client_id: str) -> None: ...

    async def on_market(
        self, books: dict[str, Book], trades: dict[str, list[Trade]], now: float
    ) -> list[ExecResult]: ...

    async def snapshot(self) -> AccountSnapshot: ...
