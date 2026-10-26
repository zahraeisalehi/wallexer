"""`log`, `reason`, `exp-reason` (cli-spec "Results and logs")."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
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


def show_log(settings: Settings, console: Console, n: int | None, full: bool = False) -> None:
    views.render_log(source.log(settings, n), console, paged=console.is_interactive, full=full)


def show_reason(settings: Settings, console: Console, n: int | None, full: bool = False) -> None:
    d = source.reason(settings, n)
    if n is None:
        views.render_reason_overview(d or {}, console, full)
    else:
        views.render_reason(d, console, full)


# ---------------------------------------------------------------------------------- exp-reason

_SECRETS = re.compile(r"(?i)(api[_-]?key|sub[_-]?account|client[_-]?id|account[_-]?id|x-api-key)")
EXP_SYSTEM = """You explain one decision of an automated spot-crypto trading bot to a hackathon judge.
You get the bot's own static decision record (features, signal values and thresholds, strategy weights,
risk-gate checks, before/after numbers, resulting orders). Explain ONLY from this record; do not invent data.
Fields: summary (2-3 sentences), why_this_action (cite the numbers and thresholds that fired),
risk (what could go wrong / what limited the risk), confidence (low|medium|high in the decision's soundness)."""
EXP_FIELDS = {"summary": "Summary", "why_this_action": "Why this action", "risk": "Risk"}


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


def exp_payload(record: dict[str, Any], equity_tmn: float) -> dict[str, Any]:
    """The anonymized decision record sent to the LLM (no ids, sizes as % of equity)."""
    payload = anonymize(record, equity_tmn)
    for o in payload.get("orders", []):
        o.pop("client_id", None)
        if o.get("quantity"):
            o["quantity"] = "redacted (size shown as % of equity elsewhere)"
    return payload


async def explain_decision(
    settings: Settings,
    conn: sqlite3.Connection,
    decision_id: int,
    record: dict[str, Any],
    equity_tmn: float,
    on_text: Callable[[str], None] | None = None,
) -> Any:
    """One LLM call; a valid answer is stored in `explanations` (also read by /api/explain)."""
    from wallexer.agent.client import LlmClient
    from wallexer.agent.schema import Explanation, explanation_schema
    from wallexer.store.db import now_iso

    client = LlmClient(settings, conn)
    try:
        res = await client.structured(
            purpose="exp_reason",
            system=EXP_SYSTEM,
            user=json.dumps(
                exp_payload(record, equity_tmn), ensure_ascii=False, sort_keys=True, default=str
            ),
            schema=explanation_schema(),
            model=Explanation,
            schema_name="explanation",
            on_text=on_text,
        )
    finally:
        await client.close()
    if res.parsed is not None:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO explanations (decision_id, created_at, explanation,"
                " llm_call_id) VALUES (?,?,?,?)",
                (decision_id, now_iso(), res.parsed.model_dump_json(), res.llm_call_id),
            )
    return res


def render_explanation(e: dict[str, Any], n: int, label: str, console: Console) -> None:
    color = {"low": "err", "medium": "warn", "high": "ok"}.get(e.get("confidence", ""), "muted")
    console.print(
        Panel(
            f"[bold]Summary[/]\n{e['summary']}\n\n[bold]Why this action[/]\n{e['why_this_action']}\n\n"
            f"[bold]Risk[/]\n{e['risk']}\n\n[bold]Confidence[/] [{color}]{e['confidence']}[/]",
            title=f"Explanation of decision #{n} ({label})",
            expand=False,
        )
    )


async def exp_reason(settings: Settings, console: Console, n: int | None) -> None:
    from wallexer.cli import stream

    if n is None:
        decisions = (source.reason(settings, None) or {}).get("decisions") or []
        if not decisions:
            console.print(
                "No decisions yet · they appear once an engine runs (`wallexer shadowtest`)."
            )
            return
        n = decisions[-1]["id"]
    r = source.remote(settings)
    has_key = bool(
        settings.secrets.boxapi_api_key and settings.secrets.boxapi_api_key.get_secret_value()
    )
    if r:
        # The server's pre-generated answer first (demo); else the local key, if there is one.
        cached = source.api_get(r, f"/api/explain/{n}")
        if cached:
            render_explanation(
                cached["explanation"], n, f"server, {cached['created_at'][11:16]} UTC", console
            )
            return
        if not has_key:
            console.print(
                f"Decision #{n} has no server explanation yet (the demo explains each new agent "
                f"decision within ~5 min). Its static record: `reason {n}`. With your own BoxAPI "
                "key (`wallexer setup`) it is explained here."
            )
            return
    p = source.reason(settings, n)
    if p is None:
        console.print(f"[err]no decision #{n}[/] · list them with `wallexer reason`")
        return
    # The decision record comes from the local DB or the remote API; the LLM call (and its
    # llm_calls row) is local, with the local BoxAPI key.
    st, _live, _origin = source.status(settings)
    equity = float((st or {}).get("equity_tmn") or settings.cfg.account.expected_equity_tmn)
    conn = _conn(settings)
    try:
        with stream.block(f"Explaining decision #{n}", EXP_FIELDS) as on_text:
            res = await explain_decision(settings, conn, n, p, equity, on_text)
    finally:
        conn.close()
    if res.parsed is None:
        console.print(
            f"[err]LLM explanation unavailable[/] ({res.error}). The static record: `wallexer reason {n}`."
        )
        return
    render_explanation(
        res.parsed.model_dump(), n, f"{res.source}, llm_call #{res.llm_call_id}", console
    )


# ---------------------------------------------------------------------------------- export


def missing_explanations(conn: sqlite3.Connection, limit: int | None = None) -> list[int]:
    """Decision ids without a stored explanation, newest first."""
    q = (
        "SELECT d.id FROM decisions d LEFT JOIN explanations e ON e.decision_id = d.id"
        " WHERE e.decision_id IS NULL ORDER BY d.id DESC"
    )
    rows = conn.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    return [r[0] for r in rows]


async def backfill_explanations(
    settings: Settings, conn: sqlite3.Connection, equity_tmn: float, limit: int | None = None
) -> int:
    """Explain decisions that have no stored explanation yet (one LLM call each). Returns the
    number explained; failures stay missing and are retried next time."""
    done = 0
    for n in missing_explanations(conn, limit):
        rec = views.reason_data(conn, n)
        if rec is None:
            continue
        res = await explain_decision(settings, conn, n, rec, equity_tmn)
        done += res.parsed is not None
    return done


def explanations_markdown(conn: sqlite3.Connection) -> str:
    """explanations.md for the export: every decision with its exp-reason answer (anonymized:
    the LLM saw no ids, keys or absolute balances)."""
    rows = conn.execute(
        "SELECT d.id, d.ts, d.mode, d.action, d.symbol, d.strategy, d.reason, e.explanation,"
        " e.llm_call_id FROM decisions d LEFT JOIN explanations e ON e.decision_id = d.id"
        " ORDER BY d.id"
    ).fetchall()
    out = [
        "# Decision explanations",
        "",
        "Every decision with its `exp-reason` explanation (summary, why, risk, confidence). "
        "Generated by the LLM from the anonymized decision record only.",
        "",
    ]
    for r in rows:
        head = " · ".join(
            x
            for x in (
                f"#{r[0]} {r[3]}",
                r[4],
                r[5],
                f"{str(r[1])[:16].replace('T', ' ')} UTC",
                r[2],
            )
            if x
        )
        out += [f"## {head}", "", f"**Record:** {r[6] or '–'}", ""]
        if r[7]:
            e = json.loads(r[7])
            out += [
                f"**Summary.** {e['summary']}",
                "",
                f"**Why this action.** {e['why_this_action']}",
                "",
                f"**Risk.** {e['risk']}",
                "",
                f"**Confidence:** {e['confidence']} (llm_call #{r[8]})",
                "",
            ]
        else:
            out += ["_No explanation yet (the LLM was unavailable or it is still queued)._", ""]
    return "\n".join(out)
