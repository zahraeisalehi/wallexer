"""The LLM agent: anonymized snapshot in, validated decision out, applied shrink-only.

What the agent can change (all logged, all able only to reduce risk):
- stance "flat" zeroes every weight; weights apply only to PROMOTED strategies (others forced
  to 0) and always pass risk.clamp_weights;
- risk_multiplier: the engine uses min(engine multiplier, agent multiplier);
- pause_markets: added to an agent-owned pause set (human stops are never lifted by the agent);
- active_markets: only these may get new entries (exits always allowed); empty = no restriction;
- watch_requests: which strategy x timeframe x market variants run as extra shadow paper books
  ("paper") or are re-checked first by the next walk-forward ("recheck"). This steers which
  evidence is gathered; it never places orders.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wallexer.agent.client import CallResult, LlmClient
from wallexer.agent.schema import AgentDecision, json_schema
from wallexer.risk.guardrails import clamp_weights

SYSTEM = """You are the risk-and-allocation agent of a spot-crypto bot on Wallex (Iranian exchange) in a 2-day hackathon.
Scoring: return, max drawdown (lower is better), activity, stability. Losing nothing matters more than activity.
You receive an anonymized JSON snapshot (percentages and bps only). You decide, every 30 minutes or on risk events:
- stance: "flat" unless at least one strategy is promoted AND its evidence is still good.
- strategy_weights in [0,1]: only promoted strategies can receive weight; others are forced to 0 anyway.
- risk_multiplier in [0,1]: lower it when drawdown grows, feeds are unhealthy, volatility is extreme or the event end is near.
- pause_markets: markets with unhealthy books, extreme spreads or extreme volatility.
- active_markets: markets allowed new entries (empty list = no restriction).
- watch_requests (max 5): strategy x timeframe x market combos to gather evidence on. "paper" = run an extra live paper book
  (max 3 at a time); "recheck" = look at it first in the next walk-forward. Prefer combos whose costs (round trip bps) are lowest
  and whose OOS/shadow stats are closest to break-even. Never request basis_tmn_usdt.
Hard rules you cannot override: a deterministic risk gate runs after you and can only shrink or reject; unpromoted strategies get 0.
Keep the rationale short, concrete and numeric (cite the numbers you used). Output only JSON matching the schema."""


def anonymize_snapshot(
    status: Mapping[str, Any],
    *,
    vol_15m_bps: Mapping[str, float],
    allowed_markets: Iterable[str],
    hours_to_end: float | None,
    capital: Decimal,
) -> dict[str, Any]:
    """Engine status -> agent input. No keys, ids or absolute TMN amounts: percentages/bps only."""
    eq = Decimal(str(status.get("equity_tmn") or 0))
    exp = Decimal(str(status.get("exposure_tmn") or 0))

    def pct_of_capital(v: Any) -> float | None:
        try:
            return round(float(Decimal(str(v)) / capital * 100), 3)
        except (ArithmeticError, ValueError):
            return None

    def stats(d: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not d:
            return None
        return {
            "trades": d.get("n"),
            "net_pct_of_capital": pct_of_capital(d.get("net_tmn")),
            "profit_factor": d.get("pf"),
            "win_rate": d.get("win_rate"),
        }

    strategies = []
    for s in status.get("strategies", []):
        strategies.append(
            {
                "name": s["name"],
                "kind": "watch_book" if s.get("watch") else "strategy",
                "promoted": s.get("promoted"),
                "paper_only": s.get("paper_only"),
                "status_reason": s.get("reason"),
                "timeframe_min": s.get("timeframe"),
                "walk_forward_oos": stats(s.get("oos")),
                "shadow_paper": stats(s.get("shadow")),
                "paper_return_pct": pct_of_capital(
                    Decimal(str(s.get("paper_equity") or capital)) - capital
                )
                if s.get("paper_equity") not in (None, "–")
                else None,
                "current_weight": s.get("weight"),
            }
        )
    markets = []
    for sym, v in (status.get("feed") or {}).items():
        markets.append(
            {
                "market": sym,
                "spread_bps": v.get("spread_bps"),
                "taker_fee": v.get("taker"),
                "round_trip_bps": v.get("rt_bps"),
                "vol_15m_bps": round(vol_15m_bps.get(sym, 0.0), 2) if sym in vol_15m_bps else None,
                "book": v.get("issue") or "ok",
            }
        )
    events = {k: v for k, v in (status.get("events") or {}).items() if not k.startswith("engine.")}
    wf = status.get("walk_forward") or {}
    return {
        "mode": status.get("mode"),
        "equity_pct_of_start": round(float(eq / capital * 100), 3) if capital else None,
        "drawdown_pct": status.get("drawdown_pct"),
        "exposure_pct_of_equity": round(float(exp / eq * 100), 3) if eq else 0.0,
        "positions_pct_of_equity": {
            k: round(float(Decimal(v) / eq * 100), 3)
            for k, v in (status.get("positions") or {}).items()
        }
        if eq
        else {},
        "hard_stopped": status.get("hard_stopped"),
        "soft_stop_active": status.get("soft_stop_active"),
        "phase": status.get("phase"),
        "hours_to_event_end": None if hours_to_end is None else round(hours_to_end, 2),
        "walk_forward_run_age_h": wf.get("age_h"),
        "regime_percentiles": status.get("regime"),
        "strategies": strategies,
        "markets": markets,
        "recent_event_counts": events,
        "allowed_strategies": ["basis_tmn_usdt", "mean_reversion", "momentum"],
        "allowed_markets": sorted(allowed_markets),
        "allowed_timeframes_min": [1, 5, 15, 60],
    }


async def ask(
    client: LlmClient, snapshot: Mapping[str, Any], purpose: str = "allocate"
) -> CallResult:
    return await client.structured(
        purpose=purpose,
        system=SYSTEM,
        user=json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str),
        schema=json_schema(),
        model=AgentDecision,
        schema_name="agent_decision",
    )


@dataclass
class Applied:
    """What a decision changes, after the shrink-only rules. Logged with before -> after values."""

    weights: dict[str, Decimal]
    risk_multiplier: Decimal
    agent_paused: frozenset[str]
    active_markets: frozenset[str] | None  # None = no restriction
    watch_paper: list[tuple[str, int, str]]
    watch_recheck: list[tuple[str, int, str]]
    notes: list[str] = field(default_factory=list)


def apply_decision(
    d: AgentDecision,
    *,
    promoted: Iterable[str],
    paper_only: Iterable[str],
    max_weight: Decimal,
    engine_risk_multiplier: Decimal,
    universe: Iterable[str],
    max_watch_books: int,
) -> Applied:
    """Pure: turn a validated decision into engine effects that can only reduce risk."""
    universe_set = set(universe)
    notes: list[str] = []
    allowed = [s for s in promoted if s not in set(paper_only)]
    raw = d.strategy_weights.model_dump()
    for name, w in raw.items():
        if w > 0 and name not in allowed:
            notes.append(f"{name}: weight {w} ignored (not promoted)")
    weights = clamp_weights(raw, allowed=allowed, max_weight=max_weight)
    if d.stance == "flat":
        weights = {k: Decimal(0) for k in weights}
    rm = min(engine_risk_multiplier, Decimal(str(round(d.risk_multiplier, 6))))
    paused = frozenset(m.upper() for m in d.pause_markets if m.upper() in universe_set)
    active_raw = {m.upper() for m in d.active_markets}
    active = frozenset(active_raw & universe_set) if active_raw & universe_set else None
    if active_raw - universe_set:
        notes.append(f"unknown markets ignored: {sorted(active_raw - universe_set)}")
    paper, recheck = [], []
    for w in d.watch_requests:
        key = (w.strategy, w.timeframe, w.market.upper())
        if key[2] not in universe_set:
            notes.append(f"watch {key} ignored (market not in universe)")
            continue
        (paper if w.action == "paper" else recheck).append(key)
    return Applied(weights, rm, paused, active, paper[:max_watch_books], recheck, notes)
