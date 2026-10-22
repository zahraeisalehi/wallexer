"""Vol/trend regime classifier, calibrated by percentile ranks against stored history.

Per market on 15-min bars: trend strength = |EMA20 - EMA60| / ATR, volatility = stdev of bar
returns (bps). Live values are ranked against the distribution of the same quantities over the
stored history (all universe markets pooled), so the output is a percentile in [0, 1] and cannot
saturate the way a fixed scale did (Phase 2 showed trend = 1.0 constantly).
Strategies get a regime-fit multiplier: momentum likes trend, mean reversion likes range and
dislikes high vol.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from wallexer.data.candles import Bar
from wallexer.data.features import Features, FloatBars, market_features_at

REGIME_TF = 15
WARMUP = 61


@dataclass(frozen=True)
class RegimeRef:
    trend: list[float]  # sorted reference values
    vol: list[float]

    @property
    def size(self) -> int:
        return len(self.trend)


def _trend_strength(
    ema_fast: float | None, ema_slow: float | None, atr: float | None
) -> float | None:
    if ema_fast is None or ema_slow is None or not atr:
        return None
    return abs(ema_fast - ema_slow) / atr


def build_reference(bars: Mapping[str, Sequence[Bar]]) -> RegimeRef:
    """Distribution of trend strength and vol over the stored 15-min history (same feature code)."""
    trend: list[float] = []
    vol: list[float] = []
    zero = Decimal(0)
    for sym, bs in bars.items():
        fb = FloatBars(bs)
        for i in range(WARMUP, len(bs)):
            c = bs[i].c
            m = market_features_at(
                sym,
                "",
                fb,
                i + 1,
                ts=0.0,
                bid=c,
                ask=c,
                maker_fee=zero,
                taker_fee=zero,
                timeframe=REGIME_TF,
            )
            ts = _trend_strength(m.ema_fast, m.ema_slow, m.atr)
            if ts is not None:
                trend.append(ts)
            if m.vol_bps is not None:
                vol.append(m.vol_bps)
    return RegimeRef(sorted(trend), sorted(vol))


def _pct(sorted_vals: list[float], x: float) -> float:
    return bisect_left(sorted_vals, x) / len(sorted_vals) if sorted_vals else 0.5


def classify(f: Features | None, ref: RegimeRef | None) -> dict[str, float]:
    """`f` must be 15-min features. Returns percentile-based regime probabilities."""
    if f is None or ref is None or not ref.size:
        return {}
    tp, vp = [], []
    for m in f.markets.values():
        ts = _trend_strength(m.ema_fast, m.ema_slow, m.atr)
        if ts is not None:
            tp.append(_pct(ref.trend, ts))
        if m.vol_bps is not None:
            vp.append(_pct(ref.vol, m.vol_bps))
    trend = sum(tp) / len(tp) if tp else 0.5
    high_vol = sum(vp) / len(vp) if vp else 0.5
    return {
        "trend": round(trend, 3),
        "range": round(1 - trend, 3),
        "high_vol": round(high_vol, 3),
        "ref_points": ref.size,
    }


def fit(strategy: str, regime: dict[str, float]) -> float:
    if not regime:
        return 1.0
    if strategy == "momentum":
        return 0.5 + regime["trend"]
    if strategy == "mean_reversion":
        return (0.5 + regime["range"]) * (1 - 0.5 * regime["high_vol"])
    return 1.0
