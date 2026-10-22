"""Learning the real spot minimum order value from exchange rejections.

The minimum is not documented. We start from a placeholder (config, verified = false). When the
exchange rejects an order as too small, we parse the minimum out of the error, persist it to
data/overrides.toml with verified = true, and log an event.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from wallexer.config import Settings
from wallexer.exchange.errors import ApiError

log = structlog.get_logger(__name__)

# Persian (U+06F0..) and Arabic-Indic (U+0660..) digits → ASCII.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Persian decimal separator.
_DECIMAL_SEP = str.maketrans({"٫": "."})
_MIN_HINT = re.compile(r"min|minimum|least|حداقل|کمتر|كمتر", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _texts(message: str, fields: Any) -> list[str]:
    out = [message or ""]
    if isinstance(fields, dict):
        for v in fields.values():
            if isinstance(v, list):
                out.extend(str(x) for x in v)
            else:
                out.append(str(v))
    return out


def parse_min_from_error(message: str, fields: Any = None) -> Decimal | None:
    """Extract the minimum order value from a rejection message, or None if not clearly one.

    Only accepts text that mentions a minimum and contains exactly one distinct positive number;
    anything ambiguous returns None (we never guess silently).
    """
    numbers: set[Decimal] = set()
    for raw in _texts(message, fields):
        text = raw.translate(_DIGITS)
        if not _MIN_HINT.search(text):
            continue
        # Strip separators only between digits so "100,000" → "100000" but "a, b" is untouched.
        text = re.sub(r"(?<=\d)[,٬،](?=\d{3})", "", text).translate(_DECIMAL_SEP)
        for m in _NUMBER.findall(text):
            try:
                n = Decimal(m)
            except InvalidOperation:
                continue
            if n > 0:
                numbers.add(n)
    return numbers.pop() if len(numbers) == 1 else None


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, Decimal):
        return f'"{v}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump_toml(data: dict[str, Any], prefix: str = "") -> str:
    """Minimal TOML writer for nested tables of scalars (all overrides.toml needs)."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if prefix and scalars:
        lines.append(f"[{prefix}]")
    lines.extend(f"{k} = {_toml_value(v)}" for k, v in scalars.items())
    if scalars:
        lines.append("")
    for k, v in tables.items():
        lines.append(_dump_toml(v, f"{prefix}.{k}" if prefix else k))
    return "\n".join(lines)


def learn(
    settings: Settings,
    value_tmn: Decimal,
    raw_message: str,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """Persist a learned minimum (TMN) to overrides.toml and apply it to the live settings."""
    orders = settings.cfg.orders
    old, old_verified = orders.min_order_value_tmn, orders.min_order_verified
    path = settings.cfg.path(settings.cfg.store.overrides_path)
    current: dict[str, Any] = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    section = current.setdefault("orders", {})
    section["min_order_value_tmn"] = value_tmn
    section["min_order_verified"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Written by wallexer at runtime (learned exchange rules). Overrides config/default.toml.\n"
    path.write_text(header + _dump_toml(current), encoding="utf-8")

    orders.min_order_value_tmn = value_tmn
    orders.min_order_verified = True
    payload = {
        "old": str(old),
        "old_verified": old_verified,
        "new": str(value_tmn),
        "raw_message": raw_message,
    }
    log.warning("exchange.min_order_learned", **payload)
    if on_event:
        on_event("exchange.min_order_learned", payload)


def handle_rejection(
    settings: Settings,
    err: ApiError,
    quote_asset: str,
    usdt_tmn: Decimal | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Decimal | None:
    """If `err` is a too-small rejection, learn the minimum (in TMN). Returns the learned value."""
    found = parse_min_from_error(err.message, err.fields)
    if found is None:
        return None
    if quote_asset == "TMN":
        value_tmn = found
    elif quote_asset == "USDT" and usdt_tmn:
        # Assumption: a USDT-market rejection states the minimum in USDT.
        value_tmn = (found * usdt_tmn).quantize(Decimal(1))
    else:
        log.warning("exchange.min_order_unconvertible", quote=quote_asset, raw=err.message)
        return None
    learn(settings, value_tmn, err.message, on_event)
    return value_tmn
