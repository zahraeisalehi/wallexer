"""Guardrail limits (from config) and the LLM weight clamp."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from wallexer.config import Settings


@dataclass(frozen=True)
class Limits:
    max_position_pct: Decimal
    max_exposure_pct: Decimal
    max_order_pct: Decimal
    min_order_tmn: Decimal
    entry_multiple: Decimal
    max_open_orders: int
    daily_dd_soft_pct: Decimal
    total_dd_hard_pct: Decimal
    soft_stop_pause_s: float
    no_entry_before_end_s: float
    force_flatten_before_end_s: float
    forbidden: frozenset[str]  # data/not_allowed.txt
    unavailable: frozenset[str]  # trading-credit package unavailableMarkets
    limit_only: frozenset[str]  # universe markets without MARKET orders (half max position)

    @classmethod
    def from_settings(
        cls,
        s: Settings,
        *,
        forbidden: frozenset[str],
        unavailable: frozenset[str],
        limit_only: frozenset[str] = frozenset(),
    ) -> Limits:
        g, o = s.cfg.guardrails, s.cfg.orders
        return cls(
            max_position_pct=g.max_position_pct,
            max_exposure_pct=g.max_exposure_pct,
            max_order_pct=g.max_order_pct,
            min_order_tmn=o.min_order_value_tmn,
            entry_multiple=o.min_entry_multiple,
            max_open_orders=g.max_open_orders,
            daily_dd_soft_pct=g.daily_dd_soft_pct,
            total_dd_hard_pct=g.total_dd_hard_pct,
            soft_stop_pause_s=g.soft_stop_pause_min * 60,
            no_entry_before_end_s=g.flatten_no_entry_min * 60,
            force_flatten_before_end_s=g.flatten_force_min * 60,
            forbidden=forbidden,
            unavailable=unavailable,
            limit_only=limit_only,
        )

    def max_position_pct_for(self, symbol: str) -> Decimal:
        return self.max_position_pct / 2 if symbol in self.limit_only else self.max_position_pct


def clamp_weights(
    proposed: Mapping[str, float | Decimal],
    *,
    allowed: Iterable[str],
    max_weight: Decimal,
) -> dict[str, Decimal]:
    """Clamp strategy weights (e.g. from the LLM) to guardrail bounds.

    Unknown or not-allowed strategies get 0; each weight is clipped to [0, max_weight]; if the
    total exceeds 1 they are scaled down to sum to 1 (the rest stays in TMN). Never enlarges.
    """
    allowed_set = set(allowed)
    out: dict[str, Decimal] = {name: Decimal(0) for name in allowed_set}
    for name, w in proposed.items():
        if name not in allowed_set:
            continue
        try:
            d = Decimal(str(w))
        except ArithmeticError:
            continue
        if not d.is_finite():
            continue
        out[name] = min(max(d, Decimal(0)), max_weight)
    total = sum(out.values(), Decimal(0))
    if total > 1:
        out = {k: v / total for k, v in out.items()}
    return out


# (min, max) accepted by `config guardrail` / `autoset guardrail`. Bounds keep hand or LLM edits
# inside a sane envelope (e.g. the hard drawdown stop can never be disabled).
GUARDRAIL_BOUNDS: dict[str, tuple[float, float]] = {
    "max_position_pct": (1, 30),
    "max_exposure_pct": (0, 80),
    "max_order_pct": (1, 20),
    "stop_loss_atr_mult": (0.5, 5),
    "stop_loss_max_pct": (0.3, 5),
    "daily_dd_soft_pct": (0.5, 6),
    "soft_stop_pause_min": (5, 240),
    "total_dd_hard_pct": (1, 10),
    "max_consecutive_losses": (1, 10),
    "loss_cooldown_min": (5, 480),
    "max_open_orders": (1, 20),
    "flatten_no_entry_min": (60, 300),
    "flatten_force_min": (20, 120),
}
