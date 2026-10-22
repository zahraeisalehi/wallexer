"""Momentum: N-bar range breakout with volume and order-book confirmation.

Entry (taker, urgent: breakouts don't wait): mid above the previous N bars' high, volume ratio
>= vol_ratio_min, bid-side book imbalance >= imbalance_min, AND the expected move
(atr_target_mult x ATR, bps) exceeds the real taker round trip (2 x taker + spread) + margin.
Exit (taker): trailing stop from the peak (min(2 x ATR, 2%)), hard stop, or time stop.
"""

from __future__ import annotations

from decimal import Decimal

from wallexer.config import GuardrailsCfg, StrategyCfg
from wallexer.data.features import Features
from wallexer.exchange.models import Side
from wallexer.strategies.base import BookView, Signal, cooled_down, costs, stop_price


class Momentum:
    name = "momentum"

    def __init__(self, cfg: StrategyCfg, guard: GuardrailsCfg) -> None:
        self.cfg = cfg
        self.guard = guard

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]:
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
                elif now - pos.entry_ts >= self.cfg.time_stop_min * 60:
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
                not m.book_ok
                or sym in book.pending
                or m.hi_n is None
                or m.atr_bps is None
                or m.vol_ratio is None
                or m.imbalance is None
                or not cooled_down(book, sym, now, self.cfg.cooldown_min)
            ):
                continue
            mid = float(m.mid)
            if not (
                mid > m.hi_n
                and m.vol_ratio >= self.cfg.vol_ratio_min
                and m.imbalance >= self.cfg.imbalance_min
            ):
                continue
            edge_bps = Decimal(str(round(self.cfg.atr_target_mult * m.atr_bps, 4)))
            threshold = m.round_trip_bps(maker_entry=False) + self.cfg.edge_margin_bps
            if edge_bps <= threshold:
                continue
            stop = stop_price(m, m.ask, atr_mult, self.guard.stop_loss_max_pct)
            out.append(
                Signal(
                    self.name,
                    sym,
                    Side.BUY,
                    "entry",
                    "taker",
                    min(1.0, m.vol_ratio / (2 * self.cfg.vol_ratio_min)),
                    {
                        "rule": "mid > N-bar high, volume and imbalance confirm, ATR target > round trip + margin",
                        "mid": str(m.mid),
                        "hi_n": m.hi_n,
                        "breakout_bars": self.cfg.breakout_bars,
                        "vol_ratio": round(m.vol_ratio, 3),
                        "imbalance": round(m.imbalance, 3),
                        "atr_bps": round(m.atr_bps, 2),
                        "expected_edge_bps": f"{edge_bps:.2f}",
                        **costs(m, False, self.cfg.edge_margin_bps),
                    },
                    stop=stop,
                )
            )
        return out
