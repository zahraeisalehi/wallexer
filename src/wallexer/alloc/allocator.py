"""Baseline allocator + the hook where the Phase 3 LLM agent plugs in.

Per promoted strategy: score = trade-level Sharpe of recent net returns (backtest + shadow),
minus a drawdown penalty. Weights = softmax(score / tau) over promoted strategies, x regime fit,
capped at max_strategy_weight; unpromoted strategies get 0. Total <= 1, the rest stays TMN, so the
default (nothing promoted) is flat. The final weights always go through risk.clamp_weights.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from wallexer.alloc.promotion import PromotionStatus
from wallexer.alloc.regime import fit
from wallexer.config import AllocCfg
from wallexer.execution.paper import PaperTrade
from wallexer.risk.guardrails import clamp_weights


@dataclass(frozen=True)
class Allocation:
    baseline: dict[str, Decimal]
    agent: dict[str, Decimal] | None
    final: dict[str, Decimal]
    source: str  # baseline | agent | fallback
    scores: dict[str, float]


class AgentHook(Protocol):
    """Phase 3: the LLM agent proposes weights from a snapshot; None = no opinion / failed."""

    async def propose(self, snapshot: dict[str, Any]) -> dict[str, float] | None: ...


class NullAgent:
    async def propose(self, snapshot: dict[str, Any]) -> dict[str, float] | None:
        return None


def score(trades: Sequence[PaperTrade], capital: Decimal, drawdown_pct: Decimal) -> float:
    rets = [float(t.net_tmn / capital) for t in trades if t.kind != "conversion"]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    sharpe = mean / sd if sd > 0 else (1.0 if mean > 0 else -1.0)
    return sharpe - 0.1 * float(drawdown_pct)


def baseline_weights(
    statuses: Mapping[str, PromotionStatus],
    trades: Mapping[str, Sequence[PaperTrade]],
    drawdowns: Mapping[str, Decimal],
    regime: dict[str, float],
    cfg: AllocCfg,
    capital: Decimal,
) -> tuple[dict[str, Decimal], dict[str, float]]:
    scores = {
        name: score(
            list(trades.get(name, []))[-cfg.score_window_trades :],
            capital,
            drawdowns.get(name, Decimal(0)),
        )
        for name in statuses
    }
    promoted = [n for n, st in statuses.items() if st.promoted]
    weights = {n: Decimal(0) for n in statuses}
    if promoted:
        exps = {n: math.exp(scores[n] / cfg.tau) for n in promoted}
        total = sum(exps.values())
        for n in promoted:
            w = exps[n] / total * fit(n, regime)
            weights[n] = Decimal(str(round(w, 6)))
    final = clamp_weights(weights, allowed=promoted, max_weight=cfg.max_strategy_weight)
    for n in statuses:
        final.setdefault(n, Decimal(0))
    return final, scores


async def allocate(
    statuses: Mapping[str, PromotionStatus],
    trades: Mapping[str, Sequence[PaperTrade]],
    drawdowns: Mapping[str, Decimal],
    regime: dict[str, float],
    cfg: AllocCfg,
    capital: Decimal,
    agent: AgentHook,
    snapshot: dict[str, Any],
) -> Allocation:
    base, scores = baseline_weights(statuses, trades, drawdowns, regime, cfg, capital)
    promoted = [n for n, st in statuses.items() if st.promoted]
    proposal = await agent.propose(snapshot)
    if proposal is None:
        return Allocation(base, None, base, "baseline", scores)
    agent_w = clamp_weights(proposal, allowed=promoted, max_weight=cfg.max_strategy_weight)
    return Allocation(base, agent_w, agent_w, "agent", scores)
