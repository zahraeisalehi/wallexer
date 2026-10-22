"""Feature computation (pure). Same code for live ticks and the startup replay.

Indicators are statistics, computed in float; anything that becomes an order price or a fee stays
Decimal (bid/ask/mid, fee rates). Every market feature carries its real cost inputs (maker/taker
fee from the fee table and the current spread), so strategy thresholds never use a global constant.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from wallexer.data.candles import Bar
from wallexer.exchange.models import Book

BPS = Decimal(10000)


@dataclass(frozen=True)
class MarketFeatures:
    symbol: str
    ts: float
    bid: Decimal
    ask: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    book_ok: bool
    book_issue: str | None
    quote: str
    last_close: float | None = None
    ret_1m: float | None = None
    ret_5m: float | None = None
    ret_15m: float | None = None
    atr: float | None = None  # in price units
    atr_bps: float | None = None
    vol_bps: float | None = None  # stdev of 1-min returns, bps
    vwap: float | None = None
    vwap_z: float | None = None  # (mid - vwap) / stdev(close - vwap)
    hi_n: float | None = None  # highest high of the previous N completed bars
    lo_n: float | None = None
    vol_ratio: float | None = None  # last completed bar volume / mean of previous N
    imbalance: float | None = None  # (bid qty - ask qty) / total, top 5 levels
    ema_fast: float | None = None
    ema_slow: float | None = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * BPS if self.mid > 0 else Decimal(0)

    def round_trip_bps(self, maker_entry: bool = False) -> Decimal:
        """Real cost of entering and exiting: fees (maker or taker entry, taker exit) + spread."""
        entry_fee = self.maker_fee if maker_entry else self.taker_fee
        return (entry_fee + self.taker_fee) * BPS + self.spread_bps


@dataclass(frozen=True)
class BasisFeatures:
    base: str  # e.g. BTC
    tmn_symbol: str  # BTCTMN
    usdt_symbol: str  # BTCUSDT
    basis_bps: Decimal  # X/TMN mid vs (X/USDT mid * USDT/TMN mid) - 1
    cost_bps: Decimal  # sum of the 3 legs' taker fees + the 3 legs' spreads
    legs: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Features:
    ts: float
    markets: dict[str, MarketFeatures]
    basis: dict[str, BasisFeatures]
    usdt_tmn_bid: Decimal | None


def book_health(book: Book | None, *, age_s: float | None, stale_after_s: float) -> str | None:
    """None if the book is usable, else the reason ('missing', 'crossed', 'stale')."""
    if book is None or book.best_bid is None or book.best_ask is None:
        return "missing"
    if book.best_bid >= book.best_ask:
        return "crossed"  # crossed or locked
    if age_s is not None and age_s > stale_after_s:
        return "stale"
    return None


def imbalance(book: Book, levels: int = 5) -> float | None:
    bids = sorted(book.bid, key=lambda lv: lv.price, reverse=True)[:levels]
    asks = sorted(book.ask, key=lambda lv: lv.price)[:levels]
    bq = float(sum((lv.quantity for lv in bids), Decimal(0)))
    aq = float(sum((lv.quantity for lv in asks), Decimal(0)))
    return (bq - aq) / (bq + aq) if bq + aq > 0 else None


def _ema(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _atr(bars: Sequence[Bar], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, b in zip(bars[-n - 1 : -1], bars[-n:], strict=True):
        h, lo, pc = float(b.h), float(b.l), float(prev.c)
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs) / n


def market_features(
    symbol: str,
    quote: str,
    bars: Sequence[Bar],
    *,
    ts: float,
    bid: Decimal,
    ask: Decimal,
    maker_fee: Decimal,
    taker_fee: Decimal,
    book_issue: str | None = None,
    book_imbalance: float | None = None,
    vwap_bars: int = 60,
    breakout_bars: int = 30,
    atr_bars: int = 14,
) -> MarketFeatures:
    """Features from completed 1-min bars + the current top of book."""
    closes = [float(b.c) for b in bars]
    mid = float((bid + ask) / 2)
    f: dict[str, float | None] = {}
    if closes:
        f["last_close"] = closes[-1]
        for k, n in (("ret_1m", 1), ("ret_5m", 5), ("ret_15m", 15)):
            f[k] = (mid / closes[-1 - n] - 1) if len(closes) > n and closes[-1 - n] > 0 else None
    if len(closes) >= 31:
        rets = [b / a - 1 for a, b in zip(closes[-31:-1], closes[-30:], strict=True) if a > 0]
        if len(rets) > 2:
            m = sum(rets) / len(rets)
            f["vol_bps"] = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * 1e4
    atr = _atr(bars, atr_bars)
    if atr is not None and mid > 0:
        f["atr"], f["atr_bps"] = atr, atr / mid * 1e4
    if len(bars) >= vwap_bars:
        win = bars[-vwap_bars:]
        vol = sum(float(b.v) for b in win)
        typ = [(float(b.h) + float(b.l) + float(b.c)) / 3 for b in win]
        vwap = (
            sum(t * float(b.v) for t, b in zip(typ, win, strict=True)) / vol
            if vol > 0
            else sum(typ) / len(typ)
        )
        dev = [float(b.c) - vwap for b in win]
        sd = math.sqrt(sum(d * d for d in dev) / (len(dev) - 1)) if len(dev) > 1 else 0.0
        f["vwap"] = vwap
        f["vwap_z"] = (mid - vwap) / sd if sd > 0 else None
    if len(bars) > breakout_bars:
        prev = bars[-breakout_bars - 1 : -1]
        f["hi_n"] = max(float(b.h) for b in prev)
        f["lo_n"] = min(float(b.l) for b in prev)
        mean_v = sum(float(b.v) for b in prev) / len(prev)
        f["vol_ratio"] = float(bars[-1].v) / mean_v if mean_v > 0 else None
    f["ema_fast"] = _ema(closes, 20)
    f["ema_slow"] = _ema(closes, 60)
    return MarketFeatures(
        symbol=symbol,
        ts=ts,
        bid=bid,
        ask=ask,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        book_ok=book_issue is None,
        book_issue=book_issue,
        quote=quote,
        imbalance=book_imbalance,
        **f,  # type: ignore[arg-type]
    )


def basis_features(
    markets: dict[str, MarketFeatures], bases: Sequence[str]
) -> dict[str, BasisFeatures]:
    """Triangle X/TMN vs X/USDT x USDT/TMN for each base with all three legs present and healthy."""
    ut = markets.get("USDTTMN")
    out: dict[str, BasisFeatures] = {}
    if ut is None or not ut.book_ok:
        return out
    for base in bases:
        xt, xu = markets.get(f"{base}TMN"), markets.get(f"{base}USDT")
        if xt is None or xu is None or not (xt.book_ok and xu.book_ok):
            continue
        implied = xu.mid * ut.mid
        if implied <= 0:
            continue
        basis = (xt.mid / implied - 1) * BPS
        legs = (xt, xu, ut)
        cost = sum((m.taker_fee * BPS + m.spread_bps for m in legs), Decimal(0))
        out[base] = BasisFeatures(
            base=base,
            tmn_symbol=xt.symbol,
            usdt_symbol=xu.symbol,
            basis_bps=basis,
            cost_bps=cost,
            legs={
                m.symbol: {
                    "bid": str(m.bid),
                    "ask": str(m.ask),
                    "taker_fee": str(m.taker_fee),
                    "spread_bps": f"{m.spread_bps:.2f}",
                }
                for m in legs
            },
        )
    return out
