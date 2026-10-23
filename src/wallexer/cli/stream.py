"""Live LLM text for the full-screen app: `sum` and `exp-reason` show the answer as it arrives.

The LLM answers in JSON (strict schema), so the partial text is cut into its string fields as it
grows. Only the app installs a sink; in the plain CLI `block()` yields None, the LLM call is made
without streaming and the output is exactly as before.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

# Set by the app: sink(event, payload). Events: "start" (title), "text" (rendered markup), "end".
SINK: dict[str, Callable[[str, Any], None] | None] = {"fn": None}


def partial_fields(text: str, fields: dict[str, str]) -> dict[str, str]:
    """The string values of `fields` found so far in a JSON object that may be cut anywhere."""
    out: dict[str, str] = {}
    for key in fields:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"', text)
        if not m:
            continue
        i, raw = m.end(), []
        while i < len(text):
            c = text[i]
            if c == "\\":
                if i + 1 >= len(text):
                    break  # escape cut in half: wait for the next chunk
                raw.append(text[i : i + 2])
                i += 2
                continue
            if c == '"':
                break
            raw.append(c)
            i += 1
        s = "".join(raw)
        try:
            out[key] = json.loads(f'"{s}"')
        except ValueError:  # a \u escape cut in half
            out[key] = json.loads(f'"{s[: s.rfind(chr(92))]}"') if "\\" in s else s
    return out


def render(fields: dict[str, str], values: dict[str, str]) -> str:
    from rich.markup import escape

    return "\n\n".join(
        f"[bold]{label}[/]\n{escape(values[k])}" for k, label in fields.items() if values.get(k)
    )


@contextmanager
def block(title: str, fields: dict[str, str]) -> Iterator[Callable[[str], None] | None]:
    sink = SINK["fn"]
    if sink is None:
        yield None
        return
    sink("start", title)

    def on_text(text: str) -> None:
        sink("text", render(fields, partial_fields(text, fields)))

    try:
        yield on_text
    finally:
        sink("end", None)
