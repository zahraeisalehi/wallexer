"""Flatten planning (pure): turn every non-TMN balance, USDT included, into TMN.

Routing per asset X:
- X/TMN if it is a known, allowed market;
- else X/USDT (the resulting USDT is sold on the next planning pass);
- USDT itself via USDT/TMN.
The caller re-plans after fills until only accepted dust is left.
Each order is MARKET where the market allows it, otherwise an aggressive LIMIT
(rules.choose_exit_order); the executor re-prices unfilled LIMIT exits.
A holding (or the rounding remainder) worth less than dust_cap_tmn is accepted as dust.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from wallexer.exchange.models import Book, Side
from wallexer.exchange.rules import MarketRules, OrderRuleError, OrderSpec, choose_exit_order

ZERO = Decimal(0)


@dataclass(frozen=True)
class Dust:
    asset: str
    qty: Decimal
    value_tmn: Decimal
    why: str  # below_cap | rounding_remainder


@dataclass
class FlattenPlan:
    orders: list[OrderSpec] = field(default_factory=list)
    dust: list[Dust] = field(default_factory=list)
    unroutable: list[str] = field(default_factory=list)  # held, no allowed market (alert!)
    over_cap_remainders: list[Dust] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return not self.orders and not self.unroutable


def _route(
    asset: str, rules: Mapping[str, MarketRules], blocked: frozenset[str]
) -> MarketRules | None:
    for sym in (f"{asset}TMN", f"{asset}USDT"):
        r = rules.get(sym)
        if r is not None and sym not in blocked:
            return r
    return None


def plan_flatten(
    balances: Mapping[str, Decimal],
    books: Mapping[str, Book],
    rules: Mapping[str, MarketRules],
    *,
    decision_id: str | int,
    dust_cap_tmn: Decimal,
    slippage_pct: Decimal,
    blocked: frozenset[str] = frozenset(),
    only_assets: frozenset[str] | None = None,
) -> FlattenPlan:
    """`balances`: free quantity per asset (cancel open orders first)."""
    plan = FlattenPlan()
    usdt_book = books.get("USDTTMN")
    usdt_bid = usdt_book.best_bid if usdt_book else None
    n = 0
    # USDT last, so its sale includes nothing stale; sorted for determinism
    assets = sorted((a for a in balances if a != "TMN"), key=lambda a: (a == "USDT", a))
    for asset in assets:
        qty = balances[asset]
        if qty <= 0 or (only_assets is not None and asset not in only_assets):
            continue
        r = _route(asset, rules, blocked)
        if r is None:
            plan.unroutable.append(asset)
            continue
        book = books.get(r.symbol)
        bid = book.best_bid if book else None
        if bid is None:
            plan.unroutable.append(asset)
            continue
        px_tmn = bid if r.quote_asset == "TMN" else (bid * usdt_bid if usdt_bid else ZERO)
        value = qty * px_tmn
        if value < dust_cap_tmn:
            plan.dust.append(Dust(asset, qty, value, "below_cap"))
            continue
        sell_qty = r.round_qty(qty)
        remainder = qty - sell_qty
        if remainder > 0:
            rem = Dust(asset, remainder, remainder * px_tmn, "rounding_remainder")
            (plan.dust if rem.value_tmn < dust_cap_tmn else plan.over_cap_remainders).append(rem)
        if sell_qty <= 0:
            continue
        n += 1
        try:
            assert book is not None
            plan.orders.append(
                choose_exit_order(
                    r,
                    Side.SELL,
                    sell_qty,
                    book,
                    decision_id=decision_id,
                    n=n,
                    slippage_pct=slippage_pct,
                )
            )
        except OrderRuleError:
            plan.unroutable.append(asset)
    return plan
