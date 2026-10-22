"""Small formatting helpers for Rich output."""

from __future__ import annotations

from decimal import Decimal


def human(n: Decimal | None) -> str:
    if n is None:
        return "–"
    for unit, size in (
        ("T", Decimal(10) ** 12),
        ("B", Decimal(10) ** 9),
        ("M", Decimal(10) ** 6),
        ("K", Decimal(1000)),
    ):
        if abs(n) >= size:
            return f"{n / size:,.1f}{unit}"
    return f"{n:,.0f}"


def pct(rate: Decimal | None) -> str:
    """Fee rate 0.002 → '0.20%'."""
    return "n/a" if rate is None else f"{rate * 100:.2f}%"


def yes_no(v: bool, bad_when: bool | None = None) -> str:
    """'yes'/'no', shown in red when v == bad_when (no colour if bad_when is None)."""
    text = "yes" if v else "no"
    return f"[red]{text}[/]" if bad_when is not None and v == bad_when else text
