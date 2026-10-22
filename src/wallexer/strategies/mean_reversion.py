"""Mean reversion: fade a stretched move below the rolling VWAP (TMN markets only for now).

Entry (maker, not urgent), all of:
- VWAP z-score <= -z_entry;
- expected move >= min_reward_risk x stop distance (1.5 by default);
- expected move > the market's real round trip (maker entry + taker exit + spread) + margin.
Expected move = target - ask, where target = VWAP + exit_frac x entry deviation and
entry deviation = VWAP - ask at entry (so the exit is past VWAP, not exactly at it).
Exit (taker): bid reaches the target (deviation fixed at entry, VWAP live), the stop, or the
time stop (time_stop_bars x timeframe).
"""

from __future__ import annotations

from decimal import Decimal

from wallexer.config import GuardrailsCfg, StrategyCfg
from wallexer.data.features import Features
from wallexer.exchange.models import Side
from wallexer.strategies.base import BookView, Signal, cooled_down, costs, in_scope, stop_price


class MeanReversion:
    name = "mean_reversion"

    def __init__(self, cfg: StrategyCfg, guard: GuardrailsCfg) -> None:
        self.cfg = cfg
        self.guard = guard

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]:
        cfg = self.cfg
        out: list[Signal] = []
        for sym, m in f.markets.items():
            pos = book.positions.get(sym)
            if pos is not None:
                why = None
                target = (m.vwap + cfg.exit_frac * pos.entry_dev) if m.vwap is not None else None
                if m.bid <= pos.stop:
                    why = "stop"
                elif target is not None and float(m.bid) >= target:
                    why = "target"
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
                                "stop": str(pos.stop),
                                "target": target,
                                "vwap_z": m.vwap_z,
                            },
                        )
                    )
                continue
            if (
                not in_scope(m.quote, cfg.quotes)
                or not m.book_ok
                or sym in book.pending
                or m.vwap_z is None
                or m.vwap is None
                or m.vwap_z > -cfg.z_entry
                or not cooled_down(book, sym, now, cfg.cooldown_min)
            ):
                continue
            ask = float(m.ask)
            dev = m.vwap - ask
            if dev <= 0:
                continue
            target = m.vwap + cfg.exit_frac * dev
            move_bps = (target - ask) / ask * 1e4
            stop = stop_price(
                m, m.ask, float(self.guard.stop_loss_atr_mult), self.guard.stop_loss_max_pct
            )
            stop_bps = float((m.ask - stop) / m.ask) * 1e4
            threshold = m.round_trip_bps(maker_entry=True) + cfg.edge_margin_bps
            if (
                move_bps < cfg.min_reward_risk * stop_bps
                or Decimal(str(round(move_bps, 4))) <= threshold
            ):
                continue
            out.append(
                Signal(
                    self.name,
                    sym,
                    Side.BUY,
                    "entry",
                    "maker",
                    min(1.0, -m.vwap_z / (2 * cfg.z_entry)),
                    {
                        "rule": "z <= -z_entry; move >= 1.5 x stop; move > round trip + margin",
                        "timeframe": m.timeframe,
                        "vwap_z": round(m.vwap_z, 3),
                        "z_entry": cfg.z_entry,
                        "vwap": round(m.vwap, 8),
                        "ask": str(m.ask),
                        "target": round(target, 8),
                        "exit_frac": cfg.exit_frac,
                        "expected_move_bps": round(move_bps, 2),
                        "stop_bps": round(stop_bps, 2),
                        "reward_risk": round(move_bps / stop_bps, 2) if stop_bps else None,
                        **costs(m, True, cfg.edge_margin_bps),
                    },
                    stop=stop,
                    entry_dev=dev,
                )
            )
        return out
