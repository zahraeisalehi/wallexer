"""Risk gate. Runs last on every account-level order intent. It can only shrink or reject.

Exits are never blocked (we must always be able to reduce risk); they are only capped to what is
held. Entries pass every guardrail of architecture §3 plus the market restrictions
(not_allowed.txt and the package's unavailableMarkets: defense in depth after the universe filter).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wallexer.exchange.models import Side
from wallexer.exchange.rules import MarketRules, OrderRuleError, notional_tmn
from wallexer.risk.guardrails import Limits
from wallexer.risk.schedule import entries_allowed, phase

ZERO = Decimal(0)


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal  # expected fill price (quote currency)
    is_exit: bool
    strategy: str | None = None


@dataclass(frozen=True)
class RiskState:
    now: float
    equity_tmn: Decimal
    peak_equity_tmn: Decimal
    day_start_equity_tmn: Decimal
    positions_tmn: Mapping[str, Decimal]  # current value per market, TMN
    held_qty: Mapping[str, Decimal]  # base qty held per market
    open_orders: int
    usdt_tmn: Decimal | None
    event_end: float | None
    hard_stopped: bool = False
    soft_stop_until: float = 0.0
    paused: frozenset[str] = frozenset()
    strategy_cooldown_until: Mapping[str, float] = field(default_factory=dict)


@dataclass
class RiskResult:
    approved: bool
    qty: Decimal
    checks: list[dict[str, Any]]
    reason: str = ""

    def as_log(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "qty": str(self.qty),
            "reason": self.reason,
            "checks": self.checks,
        }


def drawdowns(s: RiskState) -> tuple[Decimal, Decimal]:
    """(total drawdown from peak %, daily drawdown from day start %)."""
    total = (
        (s.peak_equity_tmn - s.equity_tmn) / s.peak_equity_tmn * 100
        if s.peak_equity_tmn > 0
        else ZERO
    )
    daily = (
        (s.day_start_equity_tmn - s.equity_tmn) / s.day_start_equity_tmn * 100
        if s.day_start_equity_tmn > 0
        else ZERO
    )
    return max(total, ZERO), max(daily, ZERO)


def check(intent: Intent, s: RiskState, lim: Limits, rules: MarketRules) -> RiskResult:
    checks: list[dict[str, Any]] = []

    def note(name: str, ok: bool, **detail: Any) -> bool:
        checks.append({"check": name, "ok": ok, **{k: str(v) for k, v in detail.items()}})
        return ok

    def reject(reason: str) -> RiskResult:
        return RiskResult(False, ZERO, checks, reason)

    sym = intent.symbol
    restricted = sym in lim.forbidden or sym in lim.unavailable

    # ---- exits: only capped to what is held -------------------------------------------
    if intent.is_exit:
        note("market_restricted_exit_allowed", True, restricted=restricted)
        held = rules.round_qty(s.held_qty.get(sym, ZERO))
        qty = min(intent.qty, held)
        note("cap_to_held", qty == intent.qty, requested=intent.qty, held=held)
        if qty <= 0:
            return reject("nothing held to sell")
        return RiskResult(True, qty, checks)

    # ---- entries -----------------------------------------------------------------------
    if not note("not_allowed", sym not in lim.forbidden):
        return reject(f"{sym} is in not_allowed.txt")
    if not note("package_unavailable", sym not in lim.unavailable):
        return reject(f"{sym} is unavailable for the trading-credit package")
    if not note("paused", sym not in s.paused):
        return reject(f"{sym} is paused")
    if intent.side is not Side.BUY:
        return reject("spot only: entries are buys")
    total_dd, daily_dd = drawdowns(s)
    if not note(
        "hard_stop",
        not s.hard_stopped and total_dd < lim.total_dd_hard_pct,
        total_dd_pct=f"{total_dd:.2f}",
    ):
        return reject(f"hard drawdown stop ({total_dd:.2f}% >= {lim.total_dd_hard_pct}%)")
    soft_ok = s.now >= s.soft_stop_until and daily_dd < lim.daily_dd_soft_pct
    if not note("soft_stop", soft_ok, daily_dd_pct=f"{daily_dd:.2f}"):
        return reject("daily drawdown soft stop: no new entries")
    ph = phase(s.now, s.event_end, lim.no_entry_before_end_s, lim.force_flatten_before_end_s)
    if not note("event_schedule", entries_allowed(ph), phase=ph.value):
        return reject(f"end of event schedule: {ph.value}")
    if intent.strategy:
        until = s.strategy_cooldown_until.get(intent.strategy, 0.0)
        if not note("strategy_cooldown", s.now >= until):
            return reject(f"{intent.strategy} in loss cooldown")
    if not note("max_open_orders", s.open_orders < lim.max_open_orders, open=s.open_orders):
        return reject("max open orders reached")

    try:
        unit_tmn = notional_tmn(rules, Decimal(1), intent.price, s.usdt_tmn)
    except OrderRuleError as e:
        return reject(str(e))
    if unit_tmn <= 0 or s.equity_tmn <= 0:
        return reject("cannot value order")
    eq = s.equity_tmn
    pos_cap = eq * lim.max_position_pct_for(sym) / 100 - s.positions_tmn.get(sym, ZERO)
    exp_cap = eq * lim.max_exposure_pct / 100 - sum(s.positions_tmn.values(), ZERO)
    order_cap = eq * lim.max_order_pct / 100
    want = intent.qty * unit_tmn
    allowed = min(want, pos_cap, exp_cap, order_cap)
    note("max_order_value", want <= order_cap, cap_tmn=f"{order_cap:.0f}")
    note(
        "max_position", want <= pos_cap, cap_tmn=f"{pos_cap:.0f}", limit_only=sym in lim.limit_only
    )
    note("max_exposure", want <= exp_cap, cap_tmn=f"{exp_cap:.0f}")
    qty = rules.round_qty(min(intent.qty, allowed / unit_tmn)) if allowed > 0 else ZERO
    value = qty * unit_tmn
    floor = lim.min_order_tmn * lim.entry_multiple
    if not note("entry_floor", value >= floor, value_tmn=f"{value:.0f}", floor_tmn=f"{floor:.0f}"):
        return reject("order below entry floor after limits")
    return RiskResult(True, qty, checks)
