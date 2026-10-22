"""Startup replay: recent 1-min candles through the SAME strategies and paper fill model.

Seeds each strategy's backtest stats (paper trades tagged source='backtest') for the promotion
gate. Differences from live, all conservative or documented:
- every fill is taker at candle close +/- half the CURRENT spread + slippage (no maker fills);
- order-book imbalance is not available historically: the candle proxy
  (close - open) / (high - low) is used instead;
- a market is only evaluated at timestamps where it has a bar (gaps are skipped, not filled);
  the basis triangle only where all three legs have a bar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from wallexer.data.candles import Bar
from wallexer.data.features import Features, basis_features, market_features
from wallexer.exchange.fees import FeeTable
from wallexer.execution.paper import PaperBook
from wallexer.strategies.base import Strategy

WINDOW = 130  # bars kept per evaluation (>= longest indicator window + 1)
WARMUP = 61


def _proxy_imbalance(b: Bar) -> float | None:
    rng = float(b.h - b.l)
    return float(b.c - b.o) / rng if rng > 0 else 0.0


def replay(
    strategies: Sequence[Strategy],
    books: Mapping[str, PaperBook],
    bars: Mapping[str, list[Bar]],
    spreads_bps: Mapping[str, Decimal],
    quotes: Mapping[str, str],
    fees: FeeTable,
    *,
    vwap_bars: int,
    breakout_bars: int,
    atr_bars: int,
    bases: Sequence[str],
) -> int:
    """Run the replay in place on `books`. Returns the number of evaluated timestamps."""
    index = {s: {b.t: i for i, b in enumerate(bs)} for s, bs in bars.items()}
    timeline = sorted({b.t for bs in bars.values() for b in bs})
    steps = 0
    for t in timeline:
        markets = {}
        for sym, bs in bars.items():
            i = index[sym].get(t)
            if i is None or i < WARMUP:
                continue
            bar = bs[i]
            half = bar.c * spreads_bps.get(sym, Decimal(0)) / Decimal(20000)
            fee = fees.get(sym)
            markets[sym] = market_features(
                sym,
                quotes[sym],
                bs[max(0, i - WINDOW) : i + 1],
                ts=t + 60,
                bid=bar.c - half,
                ask=bar.c + half,
                maker_fee=fee.maker,
                taker_fee=fee.taker,
                book_imbalance=_proxy_imbalance(bar),
                vwap_bars=vwap_bars,
                breakout_bars=breakout_bars,
                atr_bars=atr_bars,
            )
        if not markets:
            continue
        ut = markets.get("USDTTMN")
        f = Features(t + 60, markets, basis_features(markets, bases), ut.bid if ut else None)
        now = float(t + 60)
        for strat in strategies:
            book = books[strat.name]
            book.step(strat.on_tick(f, book.view(), now), f, now, replay=True)
        steps += 1
    return steps
