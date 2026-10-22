"""Mean reversion: fade a stretched move below the rolling VWAP.

Entry (maker, not urgent): VWAP z-score <= -z_entry AND the expected reversion to VWAP, in bps
from the ask, exceeds the market's real round trip (maker entry + taker exit + spread) + margin.
Exit (taker): price back at VWAP (z >= 0), stop hit, or time stop.
"""

from __future__ import annotations

from decimal import Decimal

from wallexer.config import GuardrailsCfg, StrategyCfg
from wallexer.data.features import Features
from wallexer.exchange.models import Side
from wallexer.strategies.base import BookView, Signal, cooled_down, costs, stop_price


class MeanReversion:
    name = "mean_reversion"

    def __init__(self, cfg: StrategyCfg, guard: GuardrailsCfg) -> None:
        self.cfg = cfg
        self.guard = guard

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]:
        out: list[Signal] = []
        for sym, m in f.markets.items():
            pos = book.positions.get(sym)
            if pos is not None:
                why = None
                if m.bid <= pos.stop:
                    why = "stop"
                elif m.vwap_z is not None and m.vwap_z >= 0:
                    why = "reverted_to_vwap"
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
                                "stop": str(pos.stop),
                                "vwap_z": m.vwap_z,
                            },
                        )
                    )
                continue
            if (
                not m.book_ok
                or sym in book.pending
                or m.vwap_z is None
                or m.vwap is None
                or m.vwap_z > -self.cfg.z_entry
                or not cooled_down(book, sym, now, self.cfg.cooldown_min)
            ):
                continue
            ask = float(m.ask)
            edge_bps = Decimal(str(round((m.vwap - ask) / ask * 1e4, 4)))
            threshold = m.round_trip_bps(maker_entry=True) + self.cfg.edge_margin_bps
            if edge_bps <= threshold:
                continue
            entry_px = m.bid  # maker: rest at best bid
            stop = stop_price(
                m, entry_px, float(self.guard.stop_loss_atr_mult), self.guard.stop_loss_max_pct
            )
            out.append(
                Signal(
                    self.name,
                    sym,
                    Side.BUY,
                    "entry",
                    "maker",
                    min(1.0, -m.vwap_z / (2 * self.cfg.z_entry)),
                    {
                        "rule": "vwap_z <= -z_entry and reversion edge > round trip + margin",
                        "vwap_z": round(m.vwap_z, 3),
                        "z_entry": self.cfg.z_entry,
                        "vwap": round(m.vwap, 8),
                        "ask": str(m.ask),
                        "expected_edge_bps": f"{edge_bps:.2f}",
                        **costs(m, True, self.cfg.edge_margin_bps),
                        "atr_bps": m.atr_bps,
                    },
                    stop=stop,
                )
            )
        return out
