"""Order construction rules (pure functions, no I/O).

- Prices/quantities are rounded to the market's precision before sending.
- Normal LIMIT prices round in our favour (BUY down, SELL up); quantities always round down,
  so we never spend or sell more than we have.
- Entries must be worth at least `min_entry_multiple` x the minimum order value, so a position
  stays sellable even after losing value.
- Exits always close in full when a partial exit would leave a remainder below the minimum.
- On markets where MARKET orders are disabled, exits use an aggressive LIMIT that crosses the
  book, re-priced while unfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from wallexer.exchange.models import Book, Market, Order, OrderType, Side


class OrderRuleError(ValueError):
    """An order cannot be built within exchange/risk rules."""


@dataclass(frozen=True)
class MarketRules:
    symbol: str
    base_asset: str
    quote_asset: str
    price_precision: int
    amount_precision: int
    market_orders_enabled: bool = True

    @classmethod
    def from_market(cls, m: Market) -> MarketRules:
        return cls(
            symbol=m.symbol,
            base_asset=m.base_asset,
            quote_asset=m.quote_asset,
            price_precision=m.price_precision,
            amount_precision=m.amount_precision,
            market_orders_enabled=m.market_orders_enabled,
        )

    @property
    def price_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.price_precision)

    @property
    def qty_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.amount_precision)

    def round_price(self, price: Decimal, side: Side, *, crossing: bool = False) -> Decimal:
        """Normal limit: BUY floors, SELL ceils (never worse than intended).

        crossing=True (aggressive exits): the opposite, so rounding can only make the order
        *more* marketable, never stop it from crossing the book.
        """
        favour_down = (side is Side.BUY) != crossing
        return price.quantize(
            self.price_step, rounding=ROUND_FLOOR if favour_down else ROUND_CEILING
        )

    def round_qty(self, qty: Decimal) -> Decimal:
        return qty.quantize(self.qty_step, rounding=ROUND_FLOOR)


@dataclass(frozen=True)
class OrderSpec:
    """A fully rounded order ready for the client. Always tied to a decision."""

    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    price: Decimal | None
    client_id: str
    decision_id: str

    def to_body(self) -> dict[str, str]:
        # Wallex expects every value as a string; request field is `client_id` (snake case).
        body = {
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.type.value,
            "quantity": format(self.quantity, "f"),
            "client_id": self.client_id,
        }
        if self.price is not None:
            body["price"] = format(self.price, "f")
        return body


def make_client_id(decision_id: str | int, n: int) -> str:
    """Our idempotency key: one decision may produce several orders (n = 1, 2, ...)."""
    did = str(decision_id).strip()
    if not did:
        raise OrderRuleError("decision_id is required for every order")
    return f"wx-{did}-{n}"


def notional_tmn(
    rules: MarketRules, qty: Decimal, price: Decimal, usdt_tmn: Decimal | None = None
) -> Decimal:
    value = qty * price
    if rules.quote_asset == "TMN":
        return value
    if rules.quote_asset == "USDT":
        if not usdt_tmn or usdt_tmn <= 0:
            raise OrderRuleError(f"{rules.symbol}: USDT/TMN price needed to value the order")
        return value * usdt_tmn
    raise OrderRuleError(f"{rules.symbol}: unsupported quote asset {rules.quote_asset}")


def build_limit_order(
    rules: MarketRules,
    side: Side,
    qty: Decimal,
    price: Decimal,
    *,
    decision_id: str | int,
    n: int,
    min_value_tmn: Decimal,
    usdt_tmn: Decimal | None = None,
    entry: bool = False,
    entry_multiple: Decimal = Decimal(1),
) -> OrderSpec:
    """Round and validate a passive LIMIT order. Entries must meet the entry floor."""
    p = rules.round_price(price, side)
    q = rules.round_qty(qty)
    if p <= 0 or q <= 0:
        raise OrderRuleError(f"{rules.symbol}: price/quantity rounds to zero ({p}, {q})")
    value = notional_tmn(rules, q, p, usdt_tmn)
    floor = min_value_tmn * (entry_multiple if entry else Decimal(1))
    if value < floor:
        kind = "entry floor" if entry else "minimum order value"
        raise OrderRuleError(
            f"{rules.symbol}: order value {value:.0f} TMN below {kind} {floor:.0f}"
        )
    return OrderSpec(
        rules.symbol, side, OrderType.LIMIT, q, p, make_client_id(decision_id, n), str(decision_id)
    )


def exit_quantity(
    rules: MarketRules,
    position: Decimal,
    desired: Decimal,
    price: Decimal,
    *,
    min_value_tmn: Decimal,
    usdt_tmn: Decimal | None = None,
) -> Decimal:
    """Quantity to sell when reducing `position` by `desired`.

    Closes in full if asked for everything, or if the remainder would be worth less than the
    minimum (an unsellable remainder would break the "TMN only at the end" rule).
    Returns 0 if the partial exit itself is below the minimum (hold rather than send a reject).
    """
    full = rules.round_qty(position)
    if desired >= position:
        return full
    q = rules.round_qty(desired)
    remainder = position - q
    if notional_tmn(rules, remainder, price, usdt_tmn) < min_value_tmn:
        return full
    if notional_tmn(rules, q, price, usdt_tmn) < min_value_tmn:
        return Decimal(0)
    return q


def aggressive_price(rules: MarketRules, side: Side, book: Book, slippage_pct: Decimal) -> Decimal:
    """LIMIT price `slippage_pct` past the best opposite price, so it fills immediately."""
    slip = slippage_pct / Decimal(100)
    if side is Side.SELL:
        if book.best_bid is None:
            raise OrderRuleError(f"{rules.symbol}: no bids to sell into")
        raw = book.best_bid * (1 - slip)
    else:
        if book.best_ask is None:
            raise OrderRuleError(f"{rules.symbol}: no asks to buy from")
        raw = book.best_ask * (1 + slip)
    p = rules.round_price(raw, side, crossing=True)
    if p <= 0:
        raise OrderRuleError(f"{rules.symbol}: aggressive price rounds to zero")
    return p


def choose_exit_order(
    rules: MarketRules,
    side: Side,
    qty: Decimal,
    book: Book,
    *,
    decision_id: str | int,
    n: int,
    slippage_pct: Decimal,
) -> OrderSpec:
    """Exit order (stop-loss / flatten): MARKET where allowed, else an aggressive LIMIT.

    No local min-value check here: the minimum is an unverified estimate and an exit must never
    be blocked by it. The exchange is the authority; a rejection is learned from.
    """
    q = rules.round_qty(qty)
    if q <= 0:
        raise OrderRuleError(f"{rules.symbol}: exit quantity rounds to zero")
    cid = make_client_id(decision_id, n)
    if rules.market_orders_enabled:
        return OrderSpec(rules.symbol, side, OrderType.MARKET, q, None, cid, str(decision_id))
    price = aggressive_price(rules, side, book, slippage_pct)
    return OrderSpec(rules.symbol, side, OrderType.LIMIT, q, price, cid, str(decision_id))


@dataclass(frozen=True)
class RepriceDecision:
    spec: OrderSpec | None  # None = nothing left to fill
    exhausted: bool  # attempts reached the configured max → caller logs risk.exit_unfilled


def reprice_exit(
    rules: MarketRules,
    prev: Order,
    book: Book,
    *,
    decision_id: str | int,
    attempt: int,
    slippage_pct: Decimal,
    max_reprices: int,
) -> RepriceDecision:
    """Replacement for an unfilled aggressive exit, after it was cancelled and re-read.

    `prev` must be the order as returned by get_order *after* the cancel, so `executedQty`
    includes any fill that raced the cancel. Only the remaining quantity is re-placed.
    `attempt` is the 1-based re-price count; the new client_id uses n = attempt + 1.
    """
    side = Side(prev.side)
    remaining = rules.round_qty(prev.remaining_qty)
    if remaining <= 0:
        return RepriceDecision(None, False)
    price = aggressive_price(rules, side, book, slippage_pct)
    spec = OrderSpec(
        rules.symbol,
        side,
        OrderType.LIMIT,
        remaining,
        price,
        make_client_id(decision_id, attempt + 1),
        str(decision_id),
    )
    return RepriceDecision(spec, attempt >= max_reprices)
