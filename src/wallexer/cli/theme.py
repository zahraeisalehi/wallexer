"""One look for every command: a Rich theme, a table factory and number formatters.

Colors carry meaning only: green = ok, yellow = warning / shadow, red = error / live trading,
cyan = headers, dim = secondary text. TMN amounts always have thousands separators and are
right-aligned; bps have one decimal; percentages are signed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.theme import Theme

THEME = Theme(
    {
        "ok": "green",
        "warn": "yellow",
        "err": "bold red",
        "shadow": "bold black on yellow",
        "live": "bold white on red",
        "muted": "dim",
        "head": "bold cyan",
        "key": "bold",
    }
)


def make_console(stderr: bool = False) -> Console:
    return Console(theme=THEME, stderr=stderr)


def table(title: str | None, columns: list[tuple[str, str]], **kw: Any) -> Table:
    """columns: (name, kind) with kind in {"text", "num"}; numbers are right-aligned."""
    t = Table(title=title, header_style="head", box=box.SIMPLE_HEAVY, title_justify="left", **kw)
    for name, kind in columns:
        t.add_column(name, justify="right" if kind == "num" else "left", overflow="fold")
    return t


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "" or v == "–":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def tmn(v: Any, signed: bool = False) -> str:
    """12,345,678 (TMN, no decimals)."""
    d = _dec(v)
    if d is None:
        return "–"
    return f"{d:+,.0f}" if signed else f"{d:,.0f}"


def bps(v: Any) -> str:
    d = _dec(v)
    return "–" if d is None else f"{d:,.1f}"


def pct(v: Any, digits: int = 2, signed: bool = True) -> str:
    """Input already in percent (e.g. 1.234 → '+1.23%')."""
    d = _dec(v)
    if d is None:
        return "–"
    return f"{d:+.{digits}f}%" if signed else f"{d:.{digits}f}%"


def verdict(ok: bool, text: str) -> str:
    return f"[ok]PASS[/] {text}" if ok else f"[err]FAIL[/] {text}"


def mode_badge(mode: str | None) -> str:
    if mode == "trade":
        return "[live] LIVE TRADE [/]"
    if mode == "shadow":
        return "[shadow] SHADOW [/]"
    return "[muted]not running[/]"
