"""Typed views of Wallex responses. All money fields are Decimal.

JSON is parsed with parse_float=Decimal (depth prices/quantities arrive as JSON floats),
so no value ever passes through binary float.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _blank_to_none(v: Any) -> Any:
    # Some markets send "" for numeric fields (seen: price on inactive markets).
    return None if isinstance(v, str) and not v.strip() else v


OptDecimal = Annotated[Decimal | None, BeforeValidator(_blank_to_none)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_MARKET = "STOP_MARKET"


class Market(_Model):
    symbol: str
    base_asset: str
    quote_asset: str
    price: OptDecimal = None
    volume_24h: OptDecimal = None
    quote_volume_24h: OptDecimal = None
    change_24h: OptDecimal = None
    is_spot: bool = False
    is_tmn_based: bool = False
    is_usdt_based: bool = False
    is_zero_fee: bool = False
    amount_precision: int
    price_precision: int
    # Undocumented field, confirmed by the user: false = MARKET order type disabled on this market.
    # The market itself stays tradable (unrelated to not_allowed.txt).
    market_orders_enabled: bool = Field(default=True, alias="is_market_type_enable")


class DepthLevel(_Model):
    price: Decimal
    quantity: Decimal
    sum: OptDecimal = None


class Book(_Model):
    ask: list[DepthLevel] = []
    bid: list[DepthLevel] = []

    @property
    def best_ask(self) -> Decimal | None:
        return min((lv.price for lv in self.ask), default=None)

    @property
    def best_bid(self) -> Decimal | None:
        return max((lv.price for lv in self.bid), default=None)

    def spread_bps(self) -> Decimal | None:
        a, b = self.best_ask, self.best_bid
        if a is None or b is None or a <= 0 or b <= 0:
            return None
        mid = (a + b) / 2
        return (a - b) / mid * Decimal(10000)


class Trade(_Model):
    symbol: str | None = None
    quantity: Decimal
    price: Decimal
    sum: OptDecimal = None
    is_buy_order: bool = Field(alias="isBuyOrder")
    timestamp: str


class Candles(_Model):
    s: str
    t: list[int] = []
    o: list[Decimal] = []
    h: list[Decimal] = []
    l: list[Decimal] = []  # noqa: E741 (UDF field name)
    c: list[Decimal] = []
    v: list[Decimal] = []


class Balance(_Model):
    asset: str
    value: Decimal
    locked: Decimal = Decimal(0)
    fiat: bool = False
    is_dust: bool = False


class FeeRate(_Model):
    maker: Decimal = Field(alias="makerFeeRate")
    taker: Decimal = Field(alias="takerFeeRate")
    source: str = "exchange"  # "exchange" | "default" (config fallback)


class Fill(_Model):
    price: Decimal
    quantity: Decimal
    sum: OptDecimal = None
    fee: OptDecimal = None
    fee_asset: str | None = Field(default=None, alias="feeAsset")
    is_buyer: bool | None = Field(default=None, alias="isBuyer")
    timestamp: str | None = None


class Order(_Model):
    client_order_id: str = Field(alias="clientOrderId")
    symbol: str
    type: str
    side: str
    price: OptDecimal = None
    orig_qty: OptDecimal = Field(default=None, alias="origQty")
    executed_qty: Decimal = Field(default=Decimal(0), alias="executedQty")
    executed_price: OptDecimal = Field(default=None, alias="executedPrice")
    executed_sum: OptDecimal = Field(default=None, alias="executedSum")
    status: str | None = None
    # Status strings are undocumented; `active == False` is treated as terminal.
    active: bool | None = None
    fee: OptDecimal = None
    fills: list[Fill] = []

    @property
    def remaining_qty(self) -> Decimal:
        return max((self.orig_qty or Decimal(0)) - self.executed_qty, Decimal(0))
