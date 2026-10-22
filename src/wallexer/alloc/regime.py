"""Simple vol/trend regime classifier (no HMM in this phase).

Per market: trend strength = |EMA20 - EMA60| / ATR, volatility = stdev of 1-min returns (bps).
Averaged over the universe into probabilities {trend, range, high_vol}; strategies get a regime
fit multiplier: momentum likes trend, mean reversion likes range and dislikes high vol.
"""

from __future__ import annotations

from wallexer.data.features import Features

HIGH_VOL_FROM_BPS = 10.0  # 1-min return stdev where "high vol" starts to count
HIGH_VOL_FULL_BPS = 30.0


def classify(f: Features) -> dict[str, float]:
    trends, vols = [], []
    for m in f.markets.values():
        if m.ema_fast is not None and m.ema_slow is not None and m.atr:
            trends.append(abs(m.ema_fast - m.ema_slow) / m.atr)
        if m.vol_bps is not None:
            vols.append(m.vol_bps)
    t = sum(trends) / len(trends) if trends else 0.0
    v = sum(vols) / len(vols) if vols else 0.0
    trend = max(0.0, min(1.0, t / 3.0))
    high_vol = max(0.0, min(1.0, (v - HIGH_VOL_FROM_BPS) / (HIGH_VOL_FULL_BPS - HIGH_VOL_FROM_BPS)))
    return {
        "trend": round(trend, 3),
        "range": round(1 - trend, 3),
        "high_vol": round(high_vol, 3),
        "trend_strength": round(t, 3),
        "vol_bps": round(v, 2),
    }


def fit(strategy: str, regime: dict[str, float]) -> float:
    if strategy == "momentum":
        return 0.5 + regime["trend"]
    if strategy == "mean_reversion":
        return (0.5 + regime["range"]) * (1 - 0.5 * regime["high_vol"])
    return 1.0
