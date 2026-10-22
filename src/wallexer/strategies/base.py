"""Strategy protocol. Strategies are pure: same features + book view in → same signals out.

Spot only, so every strategy is long-only: SELL signals only close positions.
Every entry threshold is the market's real cost (fees from the fee table + current spread) plus a
margin, never a global constant. Non-urgent entries are maker (passive LIMIT at best bid).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from wallexer.data.features import Features, MarketFeatures
from wallexer.exchange.models import Side

Kind = Literal["entry", "exit", "cycle"]
Urgency = Literal["maker", "taker"]


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    entry_px: Decimal
    entry_ts: float
    stop: Decimal
    peak: Decimal  # highest bid seen since entry (for trailing exits)


@dataclass(frozen=True)
class BookView:
    """What a strategy may know about its own paper book."""

    positions: Mapping[str, Position]
    last_exit_ts: Mapping[str, float]
    pending: frozenset[str] = frozenset()  # markets with a resting maker order


@dataclass(frozen=True)
class Signal:
    strategy: str
    market: str
    side: Side
    kind: Kind
    urgency: Urgency
    strength: float  # [-1, 1], for logging/allocation
    reason: dict[str, Any]
    stop: Decimal | None = None
    legs: tuple[tuple[str, Side], ...] = field(default=())  # cycle legs, in execution order


class Strategy(Protocol):
    name: str

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]: ...


def stop_price(m: MarketFeatures, entry_px: Decimal, atr_mult: float, max_pct: Decimal) -> Decimal:
    """Architecture §3: stop = min(atr_mult x ATR, max_pct) below entry."""
    max_frac = max_pct / 100
    if m.atr_bps is not None:
        atr_frac = Decimal(str(round(atr_mult * m.atr_bps / 1e4, 8)))
        frac = min(atr_frac, max_frac)
    else:
        frac = max_frac
    return entry_px * (1 - frac)


def cooled_down(book: BookView, market: str, now: float, minutes: int) -> bool:
    last = book.last_exit_ts.get(market)
    return last is None or now - last >= minutes * 60


def costs(m: MarketFeatures, maker_entry: bool, margin_bps: Decimal) -> dict[str, str]:
    """Cost terms logged with every entry so `reason` shows the exact threshold."""
    entry_fee = m.maker_fee if maker_entry else m.taker_fee
    return {
        "entry_fee_bps": f"{entry_fee * 10000:.2f}",
        "exit_fee_bps": f"{m.taker_fee * 10000:.2f}",
        "spread_bps": f"{m.spread_bps:.2f}",
        "margin_bps": f"{margin_bps:.2f}",
        "threshold_bps": f"{m.round_trip_bps(maker_entry) + margin_bps:.2f}",
    }
