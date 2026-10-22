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


@dataclass(frozen=True, slots=True)
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
    ret_1m: float | None = None  # 1/5/15-BAR returns (named for the 1-min timeframe)
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
    timeframe: int = 1  # minutes per bar the candle indicators were computed on

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


WINDOW = (
    130  # bars an evaluation looks back at (>= longest indicator window + 1); same live/backtest
)


class FloatBars:
    """Bar series as float lists, converted once (the backtest reuses it for every step)."""

    __slots__ = ("t", "o", "h", "l", "c", "v")

    def __init__(self, bars: Sequence[Bar]) -> None:
        self.t = [b.t for b in bars]
        self.o = [float(b.o) for b in bars]
        self.h = [float(b.h) for b in bars]
        self.l = [float(b.l) for b in bars]  # noqa: E741
        self.c = [float(b.c) for b in bars]
        self.v = [float(b.v) for b in bars]

    def __len__(self) -> int:
        return len(self.c)


def _ema(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _indicators(
    fb: FloatBars, end: int, mid: float, vwap_bars: int, breakout_bars: int, atr_bars: int
) -> dict[str, float | None]:
    """All candle indicators for the bars fb[:end] (only the last WINDOW bars are used)."""
    start = max(0, end - WINDOW)
    c, h, lo, v = fb.c, fb.h, fb.l, fb.v
    n = end - start
    f: dict[str, float | None] = {}
    if n <= 0:
        return f
    f["last_close"] = c[end - 1]
    for k, m in (("ret_1m", 1), ("ret_5m", 5), ("ret_15m", 15)):  # 1/5/15-bar returns
        f[k] = (mid / c[end - 1 - m] - 1) if n > m and c[end - 1 - m] > 0 else None
    if n >= 31:
        rets = [c[j] / c[j - 1] - 1 for j in range(end - 30, end) if c[j - 1] > 0]
        if len(rets) > 2:
            mu = sum(rets) / len(rets)
            f["vol_bps"] = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * 1e4
    if n >= atr_bars + 1 and mid > 0:
        trs = [
            max(h[j] - lo[j], abs(h[j] - c[j - 1]), abs(lo[j] - c[j - 1]))
            for j in range(end - atr_bars, end)
        ]
        atr = sum(trs) / atr_bars
        f["atr"], f["atr_bps"] = atr, atr / mid * 1e4
    if n >= vwap_bars:
        rng = range(end - vwap_bars, end)
        vol = sum(v[j] for j in rng)
        typ = [(h[j] + lo[j] + c[j]) / 3 for j in rng]
        vwap = (
            sum(t * v[j] for t, j in zip(typ, rng, strict=True)) / vol
            if vol > 0
            else sum(typ) / len(typ)
        )
        sd = (
            math.sqrt(sum((c[j] - vwap) ** 2 for j in rng) / (vwap_bars - 1))
            if vwap_bars > 1
            else 0.0
        )
        f["vwap"] = vwap
        f["vwap_z"] = (mid - vwap) / sd if sd > 0 else None
    if n > breakout_bars:
        rng = range(end - 1 - breakout_bars, end - 1)
        f["hi_n"] = max(h[j] for j in rng)
        f["lo_n"] = min(lo[j] for j in rng)
        mean_v = sum(v[j] for j in rng) / breakout_bars
        f["vol_ratio"] = v[end - 1] / mean_v if mean_v > 0 else None
    closes = c[start:end]
    f["ema_fast"] = _ema(closes, 20)
    f["ema_slow"] = _ema(closes, 60)
    return f


def market_features_at(
    symbol: str,
    quote: str,
    fb: FloatBars,
    end: int,
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
    timeframe: int = 1,
) -> MarketFeatures:
    """Features from the completed bars fb[:end] (any timeframe) + the current top of book.

    The single implementation used by live ticks and by the walk-forward backtest.
    """
    f = _indicators(fb, end, float((bid + ask) / 2), vwap_bars, breakout_bars, atr_bars)
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
        timeframe=timeframe,
        **f,  # type: ignore[arg-type]
    )


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
    timeframe: int = 1,
) -> MarketFeatures:
    """Live convenience wrapper: converts the last WINDOW bars and calls market_features_at."""
    fb = FloatBars(bars[-WINDOW:])
    return market_features_at(
        symbol,
        quote,
        fb,
        len(fb),
        ts=ts,
        bid=bid,
        ask=ask,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        book_issue=book_issue,
        book_imbalance=book_imbalance,
        vwap_bars=vwap_bars,
        breakout_bars=breakout_bars,
        atr_bars=atr_bars,
        timeframe=timeframe,
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
