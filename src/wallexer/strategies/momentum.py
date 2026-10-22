"""Momentum: N-bar range breakout with volume confirmation (15-min bars by default).

Entry (taker: breakouts don't wait), all of:
- mid above the previous N bars' high;
- last bar volume >= vol_ratio_min x the N-bar mean;
- bid-side book imbalance >= imbalance_min (live book; candle proxy (c-o)/(h-l) in backtests);
- expected move (atr_target_mult x ATR, bps) >= cost_mult (2) x the real taker round trip.
Exit (taker): ATR trailing stop from the peak (min(2 x ATR, 2%)), hard stop, or time stop
(time_stop_bars x timeframe).
"""

from __future__ import annotations

from wallexer.config import GuardrailsCfg, StrategyCfg
from wallexer.data.features import Features
from wallexer.exchange.models import Side
from wallexer.strategies.base import BookView, Signal, cooled_down, costs, in_scope, stop_price


class Momentum:
    name = "momentum"

    def __init__(self, cfg: StrategyCfg, guard: GuardrailsCfg) -> None:
        self.cfg = cfg
        self.guard = guard

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]:
        cfg = self.cfg
        out: list[Signal] = []
        atr_mult = float(self.guard.stop_loss_atr_mult)
        for sym, m in f.markets.items():
            pos = book.positions.get(sym)
            if pos is not None:
                trail = stop_price(m, pos.peak, atr_mult, self.guard.stop_loss_max_pct)
                level = max(pos.stop, trail)
                why = None
                if m.bid <= level:
                    why = "trailing_stop" if trail >= pos.stop else "stop"
                elif now - pos.entry_ts >= cfg.time_stop_bars * m.timeframe * 60:
                    why = "time_stop"
                if why:
                    out.append(
                        Signal(
                            self.name,
                            sym,
                            Side.SELL,
                            "exit",
                            "taker",
                            -1.0,
                            {
                                "exit": why,
                                "bid": str(m.bid),
                                "level": str(level),
                                "peak": str(pos.peak),
                            },
                        )
                    )
                continue
            if (
                not in_scope(m.quote, cfg.quotes)
                or not m.book_ok
                or sym in book.pending
                or m.hi_n is None
                or m.atr_bps is None
                or m.vol_ratio is None
                or m.imbalance is None
                or not cooled_down(book, sym, now, cfg.cooldown_min)
            ):
                continue
            if not (
                float(m.mid) > m.hi_n
                and m.vol_ratio >= cfg.vol_ratio_min
                and m.imbalance >= cfg.imbalance_min
            ):
                continue
            move_bps = cfg.atr_target_mult * m.atr_bps
            rt = float(m.round_trip_bps(maker_entry=False))
            if move_bps < cfg.cost_mult * rt:
                continue
            stop = stop_price(m, m.ask, atr_mult, self.guard.stop_loss_max_pct)
            out.append(
                Signal(
                    self.name,
                    sym,
                    Side.BUY,
                    "entry",
                    "taker",
                    min(1.0, m.vol_ratio / (2 * cfg.vol_ratio_min)),
                    {
                        "rule": "mid > N-bar high; volume + imbalance confirm; ATR move >= 2 x round trip",
                        "timeframe": m.timeframe,
                        "mid": str(m.mid),
                        "hi_n": m.hi_n,
                        "breakout_bars": cfg.breakout_bars,
                        "vol_ratio": round(m.vol_ratio, 3),
                        "imbalance": round(m.imbalance, 3),
                        "atr_bps": round(m.atr_bps, 2),
                        "expected_move_bps": round(move_bps, 2),
                        "required_bps": round(cfg.cost_mult * rt, 2),
                        **costs(m, False, cfg.edge_margin_bps),
                    },
                    stop=stop,
                )
            )
        return out
