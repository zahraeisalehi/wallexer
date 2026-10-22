"""Promotion gate: a strategy gets live weight only after it has shown edge on paper.

Promoted iff BOTH:
- walk-forward: the IS-selected config's OUT-OF-SAMPLE stats have >= min_backtest_trades trades,
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
    oos: TradeStats | None  # selected config's out-of-sample stats from the walk-forward run
    shadow: TradeStats
    reason: str


def oos_ok(s: TradeStats | None, cfg: PromotionCfg) -> bool:
    if s is None:
        return False
    pf = s.profit_factor
    return (
        s.n >= cfg.min_backtest_trades
        and s.net > 0
        and pf is not None
        and pf > cfg.min_profit_factor
    )


def evaluate(
    strategy: str,
    oos: TradeStats | None,
    oos_reason: str,
    shadow: Sequence[PaperTrade],
    cfg: PromotionCfg,
    currently_promoted: bool,
    *,
    run_age_h: float | None,
    max_run_age_h: float,
) -> PromotionStatus:
    """Promotion needs the walk-forward OOS gate AND non-negative shadow paper trades.

    The run's age only blocks NEW promotions (older than max_run_age_h); an already-promoted
    strategy is never demoted just because the run aged. It is demoted when its rolling shadow
    PnL turns negative, or when a newer run's OOS stats fail the gate.
    """
    sh = stats(shadow)
    if strategy in cfg.paper_only:
        return PromotionStatus(
            strategy, False, True, oos, sh, "paper-only: non-atomic legs · shadow-evaluated"
        )
    recent = [t for t in shadow if t.kind != "conversion"][-cfg.demote_window_trades :]
    rolling_negative = bool(recent) and stats(recent).net < 0
    sh_ok = sh.n >= cfg.min_shadow_trades and sh.net >= 0
    if currently_promoted:
        if rolling_negative:
            return PromotionStatus(
                strategy, False, False, oos, sh, "demoted: rolling shadow net PnL < 0"
            )
        if not oos_ok(oos, cfg):
            return PromotionStatus(
                strategy, False, False, oos, sh, f"demoted: walk-forward OOS fails ({oos_reason})"
            )
        return PromotionStatus(
            strategy, True, False, oos, sh, "promoted (OOS + shadow); kept regardless of run age"
        )
    if run_age_h is None:
        return PromotionStatus(
            strategy,
            False,
            False,
            oos,
            sh,
            "no walk-forward run: `wallexer backtest --walk-forward`",
        )
    if not oos_ok(oos, cfg):
        return PromotionStatus(strategy, False, False, oos, sh, f"OOS: {oos_reason}")
    if run_age_h > max_run_age_h:
        return PromotionStatus(
            strategy,
            False,
            False,
            oos,
            sh,
            f"walk-forward run {run_age_h:.0f}h old > {max_run_age_h:.0f}h: rerun",
        )
    if not sh_ok:
        return PromotionStatus(
            strategy,
            False,
            False,
            oos,
            sh,
            f"OOS passes; shadow {sh.n}/{cfg.min_shadow_trades} trades, net {sh.net:.0f} (need >= 0)",
        )
    return PromotionStatus(
        strategy, True, False, oos, sh, "walk-forward OOS and shadow gates passed"
    )
