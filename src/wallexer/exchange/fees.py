"""Fee table from /v1/account/fee, tolerant of the shapes the account actually returns.

Documented: result = {SYMBOL: {makerFeeRate, takerFeeRate, ...}}. Observed on the trading-credit
account: result = [] (and entries can be [] too). Anything unusable means "no fee data" for that
market, and the config default (0.30%) is used instead, marked source = "default", so PnL/edge
calculations always have a fee and we can see where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from wallexer.exchange.models import FeeRate

SOURCE_EXCHANGE = "exchange"
SOURCE_DEFAULT = "default"
# Non-market keys seen in result on the trading-credit account (2026-10-22):
# "default": [] (the value that crashed doctor) and "metaData": {levels, coin_levels,
# latestTradesSum, latestTetherTradesSum} (fee-tier info). Kept out of the per-market table.
NON_MARKET_KEYS = frozenset({"default", "metaData"})


def describe_shape(result: Any) -> dict[str, Any]:
    """Structure only (types, counts, a few symbols), never values, for a one-time log line."""
    shape: dict[str, Any] = {"type": type(result).__name__}
    if isinstance(result, dict):
        shape["len"] = len(result)
        shape["sample_keys"] = list(result)[:5]
        if result:
            first = next(iter(result.values()))
            shape["entry_type"] = type(first).__name__
            if isinstance(first, dict):
                shape["entry_keys"] = sorted(first)
    elif isinstance(result, list):
        shape["len"] = len(result)
        if result:
            shape["entry_type"] = type(result[0]).__name__
    return shape


@dataclass
class FeeTable:
    rates: dict[str, FeeRate]  # markets with usable exchange data
    default: FeeRate
    skipped: list[str] = field(default_factory=list)  # symbols whose entries were unusable
    meta_keys: list[str] = field(default_factory=list)  # non-market keys present (not fees)
    raw_shape: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> str:
        """'exchange' if every fee came from the exchange, 'default' if none did, else 'mixed'."""
        if not self.rates:
            return SOURCE_DEFAULT
        return "mixed" if self.skipped else SOURCE_EXCHANGE

    def get(self, symbol: str) -> FeeRate:
        return self.rates.get(symbol) or self.default


def parse_fee_table(result: Any, default_maker: Decimal, default_taker: Decimal) -> FeeTable:
    default = FeeRate(makerFeeRate=default_maker, takerFeeRate=default_taker, source=SOURCE_DEFAULT)
    table = FeeTable(rates={}, default=default, raw_shape=describe_shape(result))
    if not isinstance(result, dict):  # [], None, missing, or anything else → no fee data
        return table
    for symbol, entry in result.items():
        if symbol in NON_MARKET_KEYS:
            table.meta_keys.append(symbol)
            continue
        if not isinstance(entry, dict) or not entry:
            table.skipped.append(symbol)
            continue
        try:
            table.rates[symbol] = FeeRate.model_validate(entry)
        except ValidationError:
            table.skipped.append(symbol)
    return table
