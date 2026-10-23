"""`log`, `reason`, `exp-reason` (cli-spec "Results and logs")."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from rich.console import Console
from rich.panel import Panel

from wallexer.cli import source, views
from wallexer.config import Settings
from wallexer.store.db import init_db


def _conn(settings: Settings) -> sqlite3.Connection:
    return init_db(settings.cfg.path(settings.cfg.store.db_path))


# ---------------------------------------------------------------------------------- log + reason
# Data comes from the local DB or, in remote mode, the server's read-only API (cli/source.py).


def show_log(settings: Settings, console: Console, n: int | None) -> None:
    views.render_log(source.log(settings, n), console, paged=console.is_terminal)


def show_reason(settings: Settings, console: Console, n: int | None) -> None:
    d = source.reason(settings, n)
    if n is None:
        views.render_reason_overview(d or {}, console)
    else:
        views.render_reason(d, console)


# ---------------------------------------------------------------------------------- exp-reason

_SECRETS = re.compile(r"(?i)(api[_-]?key|sub[_-]?account|client[_-]?id|account[_-]?id|x-api-key)")
EXP_SYSTEM = """You explain one decision of an automated spot-crypto trading bot to a hackathon judge.
You get the bot's own static decision record (features, signal values and thresholds, strategy weights,
risk-gate checks, before/after numbers, resulting orders). Explain ONLY from this record; do not invent data.
Fields: summary (2-3 sentences), why_this_action (cite the numbers and thresholds that fired),
risk (what could go wrong / what limited the risk), confidence (low|medium|high in the decision's soundness)."""


def anonymize(obj: Any, equity_tmn: float) -> Any:
    """Drop ids/keys; turn absolute TMN amounts into % of equity. Public market data stays."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRETS.search(k) or k in ("prompt_hash", "llm_call_id", "run_id"):
                continue
            if k.endswith("_tmn") and equity_tmn:
                try:
                    out[k.replace("_tmn", "_pct_of_equity")] = round(float(v) / equity_tmn * 100, 4)
                    continue
                except (TypeError, ValueError):
                    pass
            out[k] = anonymize(v, equity_tmn)
        return out
    if isinstance(obj, list):
        return [anonymize(x, equity_tmn) for x in obj]
    return obj


async def exp_reason(settings: Settings, console: Console, n: int | None) -> None:
    from wallexer.agent.client import LlmClient
    from wallexer.agent.schema import Explanation, explanation_schema

    # The decision record comes from the local DB or the remote API; the LLM call (and its
    # llm_calls row) is always local, with the local BoxAPI key.
    if n is None:
        decisions = (source.reason(settings, None) or {}).get("decisions") or []
        if not decisions:
            console.print(
                "No decisions yet · they appear once an engine runs (`wallexer shadowtest`)."
            )
            return
        n = decisions[-1]["id"]
    p = source.reason(settings, n)
    if p is None:
        console.print(f"[err]no decision #{n}[/] · list them with `wallexer reason`")
        return
    st, _live, _origin = source.status(settings)
    equity = float((st or {}).get("equity_tmn") or settings.cfg.account.expected_equity_tmn)
    conn = _conn(settings)
    try:
        payload = anonymize(p, equity)
        for o in payload.get("orders", []):
            o.pop("client_id", None)
            if o.get("quantity"):
                o["quantity"] = "redacted (size shown as % of equity elsewhere)"
        client = LlmClient(settings, conn)
        res = await client.structured(
            purpose="exp_reason",
            system=EXP_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            schema=explanation_schema(),
            model=Explanation,
            schema_name="explanation",
        )
        await client.close()
    finally:
        conn.close()
    if res.parsed is None:
        console.print(
            f"[err]LLM explanation unavailable[/] ({res.error}). The static record: `wallexer reason {n}`."
        )
        return
    e: Explanation = res.parsed  # type: ignore[assignment]
    color = {"low": "err", "medium": "warn", "high": "ok"}[e.confidence]
    console.print(
        Panel(
            f"[bold]Summary[/]\n{e.summary}\n\n[bold]Why this action[/]\n{e.why_this_action}\n\n"
            f"[bold]Risk[/]\n{e.risk}\n\n[bold]Confidence[/] [{color}]{e.confidence}[/]",
            title=f"Explanation of decision #{n} ({res.source}, llm_call #{res.llm_call_id})",
            expand=False,
        )
    )
