"""Account-level USDT inventory (architecture.md "USDT inventory").

Real USDT stays 0 until a USDT-quoted strategy is promoted. Then:
target fraction of equity = sum over promoted USDT-quoted strategies of
(weight x that strategy's USDT-market exposure fraction), but 0 if its expected trades per
conversion cycle are below break-even (~2). Conversions only when |target - current| exceeds the
band and the last conversion is older than the minimum hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class UsdtDecision:
    target_frac: Decimal
    current_frac: Decimal
    delta_frac: Decimal  # > 0: buy USDT, < 0: sell USDT, 0: hold
    reason: str


def target_frac(
    weights: Mapping[str, Decimal],
    usdt_exposure: Mapping[str, Decimal],  # per strategy: USDT-market exposure / book equity
    trades_per_cycle: Mapping[str, Decimal],
    breakeven_trades: Decimal,
) -> Decimal:
    total = ZERO
    for name, w in weights.items():
        if w <= 0 or trades_per_cycle.get(name, ZERO) < breakeven_trades:
            continue
        total += w * usdt_exposure.get(name, ZERO)
    return min(total, Decimal(1))


def decide(
    target: Decimal,
    current: Decimal,
    *,
    now: float,
    last_conversion_ts: float | None,
    band_pct: Decimal,
    min_hold_s: float,
) -> UsdtDecision:
    gap = target - current
    if abs(gap) * 100 <= band_pct:
        return UsdtDecision(target, current, ZERO, f"within band ({band_pct}%)")
    if last_conversion_ts is not None and now - last_conversion_ts < min_hold_s and target > 0:
        # Allow selling back to 0 at any time (risk reduction); otherwise respect the hold.
        return UsdtDecision(target, current, ZERO, "min hold not reached")
    return UsdtDecision(target, current, gap, "rebalance")
