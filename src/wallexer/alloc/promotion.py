"""Promotion gate: a strategy gets live weight only after it has shown edge on paper.

Promoted iff BOTH:
- backtest (startup replay of recent candles): >= min_backtest_trades closed trades,
  net PnL > 0 and profit factor > min_profit_factor; AND
- shadow (paper on live data): >= min_shadow_trades closed trades and net PnL >= 0.
Demoted when the last `demote_window_trades` shadow trades are net-negative.
Paper-only strategies (non-atomic legs) are never promoted.
Conversion rows (USDT cycles) count towards net PnL but not towards the trade count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from wallexer.config import PromotionCfg
from wallexer.execution.paper import PaperTrade

ZERO = Decimal(0)


@dataclass(frozen=True)
class TradeStats:
    n: int
    net: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    wins: int

    @property
    def profit_factor(self) -> Decimal | None:
        if self.gross_loss > 0:
            return self.gross_profit / self.gross_loss
        return None if self.gross_profit == 0 else Decimal("Infinity")

    def as_dict(self) -> dict[str, str | int]:
        pf = self.profit_factor
        return {
            "n": self.n,
            "net_tmn": f"{self.net:.0f}",
            "pf": "–" if pf is None else ("inf" if pf.is_infinite() else f"{pf:.2f}"),
            "win_rate": f"{(self.wins / self.n * 100):.0f}%" if self.n else "–",
        }


def stats(trades: Sequence[PaperTrade]) -> TradeStats:
    n = wins = 0
    net = gp = gl = ZERO
    for t in trades:
        net += t.net_tmn
        if t.kind != "conversion":
            n += 1
            wins += t.net_tmn > 0
        if t.net_tmn > 0:
            gp += t.net_tmn
        else:
            gl -= t.net_tmn
    return TradeStats(n, net, gp, gl, wins)


@dataclass(frozen=True)
class PromotionStatus:
    strategy: str
    promoted: bool
    paper_only: bool
    backtest: TradeStats
    shadow: TradeStats
    reason: str


def evaluate(
    strategy: str,
    backtest: Sequence[PaperTrade],
    shadow: Sequence[PaperTrade],
    cfg: PromotionCfg,
    currently_promoted: bool,
) -> PromotionStatus:
    bt, sh = stats(backtest), stats(shadow)
    if strategy in cfg.paper_only:
        return PromotionStatus(strategy, False, True, bt, sh, "paper-only: non-atomic legs")
    pf = bt.profit_factor
    bt_ok = (
        bt.n >= cfg.min_backtest_trades
        and bt.net > 0
        and pf is not None
        and pf > cfg.min_profit_factor
    )
    sh_ok = sh.n >= cfg.min_shadow_trades and sh.net >= 0
    recent = [t for t in shadow if t.kind != "conversion"][-cfg.demote_window_trades :]
    rolling_negative = bool(recent) and stats(recent).net < 0
    if currently_promoted and rolling_negative:
        return PromotionStatus(
            strategy, False, False, bt, sh, "demoted: rolling shadow net PnL < 0"
        )
    if not bt_ok:
        why = (
            f"backtest {bt.n}/{cfg.min_backtest_trades} trades, net {bt.net:.0f}, "
            f"PF {bt.as_dict()['pf']} (need > {cfg.min_profit_factor})"
        )
        return PromotionStatus(strategy, False, False, bt, sh, why)
    if not sh_ok:
        why = f"shadow {sh.n}/{cfg.min_shadow_trades} trades, net {sh.net:.0f} (need >= 0)"
        return PromotionStatus(strategy, False, False, bt, sh, why)
    return PromotionStatus(strategy, True, False, bt, sh, "backtest and shadow gates passed")
