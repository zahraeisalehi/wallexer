"""Triangle basis: X/TMN vs X/USDT x USDT/TMN.

A cycle starts and ends in TMN, so it needs no inventory:
- X cheap on TMN (basis < 0): TMN -> X (buy X/TMN) -> USDT (sell X/USDT) -> TMN (sell USDT/TMN)
- X rich on TMN (basis > 0):  TMN -> USDT (buy USDT/TMN) -> X (buy X/USDT) -> TMN (sell X/TMN)
Threshold = sum of the three legs' taker fees + the three legs' spreads (real fee table) + margin.

Paper-only in live mode: the legs are not atomic, and a failed leg would leave inventory.
Live trading needs leg-failure unwinding first (Phase 3).
"""

from __future__ import annotations

from wallexer.config import StrategyCfg
from wallexer.data.features import Features
from wallexer.exchange.models import Side
from wallexer.strategies.base import BookView, Signal, cooled_down


class BasisTmnUsdt:
    name = "basis_tmn_usdt"
    bases = ("BTC", "ETH", "XAUT")

    def __init__(self, cfg: StrategyCfg) -> None:
        self.cfg = cfg

    def on_tick(self, f: Features, book: BookView, now: float) -> list[Signal]:
        out: list[Signal] = []
        for base, b in f.basis.items():
            threshold = b.cost_bps + self.cfg.edge_margin_bps
            if abs(b.basis_bps) <= threshold:
                continue
            if not cooled_down(book, b.tmn_symbol, now, self.cfg.cooldown_min):
                continue
            if b.basis_bps < 0:
                legs = (
                    (b.tmn_symbol, Side.BUY),
                    (b.usdt_symbol, Side.SELL),
                    ("USDTTMN", Side.SELL),
                )
                direction = "buy_tmn_leg"
            else:
                legs = (("USDTTMN", Side.BUY), (b.usdt_symbol, Side.BUY), (b.tmn_symbol, Side.SELL))
                direction = "sell_tmn_leg"
            out.append(
                Signal(
                    self.name,
                    b.tmn_symbol,
                    Side.BUY if b.basis_bps < 0 else Side.SELL,
                    "cycle",
                    "taker",
                    max(-1.0, min(1.0, float(-b.basis_bps / (2 * threshold)))),
                    {
                        "rule": "|basis| > sum(3 taker fees + 3 spreads) + margin",
                        "base": base,
                        "direction": direction,
                        "basis_bps": f"{b.basis_bps:.2f}",
                        "cost_bps": f"{b.cost_bps:.2f}",
                        "margin_bps": f"{self.cfg.edge_margin_bps:.2f}",
                        "legs": b.legs,
                    },
                    legs=legs,
                )
            )
        return out
