"""Walk-forward evaluation: tune in-sample (oldest 2/3), judge out-of-sample (newest 1/3).

- Same strategies, PaperBook, fill model and real fee table as live trading. Every fill is taker
  at close +/- half the market's representative spread + slippage (maker fills cannot be
  modelled on candles; taker is the conservative choice).
- Features come from the single live implementation (features.market_features_at); they are
  computed once per (timeframe, market, feature variant) and reused by every grid config.
- Order-book imbalance is not available historically: the candle proxy (c - o)/(h - l) is used.
- IS and OOS run in separate, fresh books; open positions are closed at each segment end.
- Selection is IS-only: per strategy, the (timeframe, params) with the best IS net PnL among
  configs with >= min_is_trades IS trades and IS PF > 1. OOS never influences the choice.
- The promotion gate then uses only the selected config's OOS stats.
- basis_tmn_usdt is not evaluated here (its legs need simultaneous books; shadow only).
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wallexer.alloc.promotion import TradeStats, stats
from wallexer.config import Settings, StrategyCfg
from wallexer.data.candles import Bar
from wallexer.data.features import Features, FloatBars, MarketFeatures, market_features_at
from wallexer.exchange.fees import FeeTable
from wallexer.exchange.rules import MarketRules
from wallexer.execution.paper import FillModel, PaperBook, PaperTrade
from wallexer.strategies.mean_reversion import MeanReversion
from wallexer.strategies.momentum import Momentum

WARMUP = 61  # bars before the first evaluation (longest indicator window + 1)
STRATEGIES = ("mean_reversion", "momentum")


@dataclass
class SegmentResult:
    trades: list[PaperTrade]

    def by_market(self) -> dict[str, list[PaperTrade]]:
        out: dict[str, list[PaperTrade]] = {}
        for t in self.trades:
            if t.kind == "trade":
                out.setdefault(t.market, []).append(t)
        return out


@dataclass
class ConfigResult:
    strategy: str
    timeframe: int
    params: dict[str, float]
    is_: SegmentResult
    oos: SegmentResult

    @property
    def is_stats(self) -> TradeStats:
        return stats(self.is_.trades)

    @property
    def oos_stats(self) -> TradeStats:
        return stats(self.oos.trades)


@dataclass
class WalkForward:
    history_from: int
    history_to: int
    split_ts: int
    grid_size: int
    grids: dict[str, Any]
    spreads: dict[str, Any]
    results: list[ConfigResult] = field(default_factory=list)
    selected: dict[str, ConfigResult | None] = field(default_factory=dict)
    seconds: float = 0.0


def max_drawdown_pct(trades: Sequence[PaperTrade], capital: Decimal) -> Decimal:
    """Peak-to-trough of capital + cumulative realised net PnL (trade order), % of the peak."""
    eq = peak = capital
    worst = Decimal(0)
    for t in sorted(trades, key=lambda x: x.exit_ts):
        eq += t.net_tmn
        peak = max(peak, eq)
        worst = max(worst, (peak - eq) / peak * 100)
    return worst


def _proxy_imbalance(fb: FloatBars, i: int) -> float:
    rng = fb.h[i] - fb.l[i]
    return (fb.c[i] - fb.o[i]) / rng if rng > 0 else 0.0


def build_features(
    bars: Mapping[str, Sequence[Bar]],
    quotes: Mapping[str, str],
    fees: FeeTable,
    spreads_bps: Mapping[str, Decimal],
    *,
    timeframe: int,
    vwap_bars: int,
    breakout_bars: int,
    atr_bars: int,
) -> list[Features]:
    """Features at each bar close (t + timeframe), using only bars that closed by then."""
    per_ts: dict[int, dict[str, MarketFeatures]] = {}
    for sym, bs in bars.items():
        fb = FloatBars(bs)
        fee = fees.get(sym)
        spread = spreads_bps.get(sym, Decimal(0))
        for i in range(WARMUP, len(bs)):
            close = bs[i].c
            half = close * spread / Decimal(20000)
            ts = bs[i].t + timeframe * 60
            per_ts.setdefault(ts, {})[sym] = market_features_at(
                sym,
                quotes[sym],
                fb,
                i + 1,
                ts=ts,
                bid=close - half,
                ask=close + half,
                maker_fee=fee.maker,
                taker_fee=fee.taker,
                book_imbalance=_proxy_imbalance(fb, i),
                vwap_bars=vwap_bars,
                breakout_bars=breakout_bars,
                atr_bars=atr_bars,
                timeframe=timeframe,
            )
    out = []
    for ts in sorted(per_ts):
        ms = per_ts[ts]
        ut = ms.get("USDTTMN")
        out.append(Features(float(ts), ms, {}, ut.bid if ut else None))
    return out


def _book(settings: Settings, rules: dict[str, MarketRules], source: str) -> PaperBook:
    p, o = settings.cfg.paper, settings.cfg.orders
    return PaperBook(
        "wf",
        capital_tmn=p.capital_tmn,
        position_frac=p.position_frac,
        fill=FillModel(p.slippage_bps),
        rules=rules,
        min_entry_tmn=o.min_order_value_tmn * o.min_entry_multiple,
        maker_timeout_s=p.maker_timeout_s,
        usdt_min_hold_s=settings.cfg.alloc.usdt_min_hold_min * 60,
        source=source,
    )


def _segment(strategy: Any, feats: Sequence[Features], book: PaperBook) -> SegmentResult:
    for f in feats:
        book.step(strategy.on_tick(f, book.view(), f.ts), f, f.ts, replay=True)
    if feats:
        book.close_all(feats[-1], feats[-1].ts, "segment_end")
    return SegmentResult(list(book.closed))


def _make(name: str, cfg: StrategyCfg, settings: Settings) -> Any:
    g = settings.cfg.guardrails
    return MeanReversion(cfg, g) if name == "mean_reversion" else Momentum(cfg, g)


def grid_configs(settings: Settings) -> list[tuple[str, int, dict[str, float]]]:
    b = settings.cfg.backtest
    out = []
    for name, grid in (("mean_reversion", b.grid_mean_reversion), ("momentum", b.grid_momentum)):
        keys = sorted(grid)
        for tf in b.timeframes:
            for combo in itertools.product(*(grid[k] for k in keys)):
                out.append((name, tf, dict(zip(keys, combo, strict=True))))
    return out


def select(results: Sequence[ConfigResult], strategy: str, min_trades: int) -> ConfigResult | None:
    """IS-only choice: best IS net PnL with >= min_trades and PF > 1; ties -> fewer trades."""
    ok = []
    for r in results:
        if r.strategy != strategy:
            continue
        s = r.is_stats
        pf = s.profit_factor
        if s.n >= min_trades and pf is not None and pf > 1:
            ok.append(r)
    if not ok:
        return None
    return max(ok, key=lambda r: (r.is_stats.net, -r.is_stats.n))


def run(
    settings: Settings,
    bars_by_tf: Mapping[int, Mapping[str, Sequence[Bar]]],
    quotes: Mapping[str, str],
    rules: dict[str, MarketRules],
    fees: FeeTable,
    spreads_bps: Mapping[str, Decimal],
    spreads_meta: dict[str, Any],
    progress: Any = None,
) -> WalkForward:
    t0 = time.time()
    b = settings.cfg.backtest
    one = bars_by_tf[min(bars_by_tf)]
    h_from = max(bs[0].t for bs in one.values() if bs)
    h_to = min(bs[-1].t for bs in one.values() if bs)
    split = int(h_from + (h_to - h_from) * b.split_frac)
    configs = grid_configs(settings)
    wf = WalkForward(
        h_from,
        h_to,
        split,
        len(configs),
        {
            "mean_reversion": b.grid_mean_reversion,
            "momentum": b.grid_momentum,
            "timeframes": b.timeframes,
            "split_frac": b.split_frac,
            "min_is_trades": b.min_is_trades,
        },
        spreads_meta,
    )
    sc = settings.cfg.strategies
    for tf in b.timeframes:
        variants = sorted(
            {int(p["breakout_bars"]) for n, t, p in configs if t == tf and "breakout_bars" in p}
            | {sc.momentum.breakout_bars}
        )
        feats_by_variant = {
            bb: build_features(
                bars_by_tf[tf],
                quotes,
                fees,
                spreads_bps,
                timeframe=tf,
                vwap_bars=sc.mean_reversion.vwap_bars,
                breakout_bars=bb,
                atr_bars=sc.momentum.atr_bars,
            )
            for bb in variants
        }
        for name, ctf, params in configs:
            if ctf != tf:
                continue
            base = getattr(sc, name)
            upd: dict[str, Any] = {"timeframe": tf, **params}
            if "breakout_bars" in upd:
                upd["breakout_bars"] = int(upd["breakout_bars"])
            cfg = base.model_copy(update=upd)
            # mean_reversion ignores hi_n/vol_ratio, so any variant serves it
            feats = feats_by_variant.get(cfg.breakout_bars) or feats_by_variant[variants[0]]
            is_f = [f for f in feats if f.ts <= split]
            oos_f = [f for f in feats if f.ts > split]
            res = ConfigResult(
                name,
                tf,
                params,
                _segment(_make(name, cfg, settings), is_f, _book(settings, rules, "backtest_is")),
                _segment(_make(name, cfg, settings), oos_f, _book(settings, rules, "backtest_oos")),
            )
            wf.results.append(res)
            if progress:
                progress(len(wf.results), len(configs))
    for name in STRATEGIES:
        wf.selected[name] = select(wf.results, name, b.min_is_trades)
    wf.seconds = round(time.time() - t0, 1)
    return wf


def best_per_timeframe(
    wf: WalkForward, strategy: str, tf: int, min_trades: int
) -> ConfigResult | None:
    """Report helper: IS-valid best for this timeframe, else the best IS net (flagged by caller)."""
    rs = [r for r in wf.results if r.strategy == strategy and r.timeframe == tf]
    chosen = select(rs, strategy, min_trades)
    if chosen is None and rs:
        chosen = max(rs, key=lambda r: (r.is_stats.net, -r.is_stats.n))
    return chosen
