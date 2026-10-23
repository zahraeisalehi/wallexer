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

# One palette for the plain CLI and the full-screen app (cli/tui.py uses the same hex values).
PALETTE = {
    "ok": "#3fb950",
    "warn": "#d29922",
    "err": "#f85149",
    "shadow": "#e3b341",  # shadow mode: yellow
    "live": "#da3633",  # live trading: red
    "demo": "#388bfd",  # public demo: blue
    "head": "#39c5cf",
    "muted": "#8b949e",
    "bg": "#0d1117",
    "panel": "#161b22",
    "fg": "#e6edf3",
}

THEME = Theme(
    {
        "ok": PALETTE["ok"],
        "warn": PALETTE["warn"],
        "err": f"bold {PALETTE['err']}",
        "shadow": f"bold black on {PALETTE['shadow']}",
        "live": f"bold white on {PALETTE['live']}",
        "demo": f"bold white on {PALETTE['demo']}",
        "muted": "dim",
        "head": f"bold {PALETTE['head']}",
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


def duration(v: Any) -> str:
    """Seconds → '1d 14h 27m 43s' (leading zero units dropped: '3m 05s', '42s')."""
    try:
        s = int(float(v))
    except (TypeError, ValueError):
        return "–"
    d, s = divmod(max(s, 0), 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def verdict(ok: bool, text: str) -> str:
    return f"[ok]PASS[/] {text}" if ok else f"[err]FAIL[/] {text}"


def mode_badge(mode: str | None) -> str:
    if mode == "trade":
        return "[live] LIVE TRADE [/]"
    if mode == "shadow":
        return "[shadow] SHADOW [/]"
    return "[muted]not running[/]"
