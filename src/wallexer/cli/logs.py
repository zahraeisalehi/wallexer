"""`log`, `reason`, `exp-reason` (cli-spec "Results and logs")."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from wallexer.config import Settings
from wallexer.store.db import init_db


def _conn(settings: Settings) -> sqlite3.Connection:
    return init_db(settings.cfg.path(settings.cfg.store.db_path))


def _j(s: str | None) -> Any:
    try:
        return json.loads(s) if s else None
    except ValueError:
        return s


# ---------------------------------------------------------------------------------- log


def show_log(settings: Settings, console: Console, n: int | None) -> None:
    conn = _conn(settings)
    try:
        if n is not None:
            row = conn.execute(
                "SELECT id, ts, kind, payload FROM events WHERE id=?", (n,)
            ).fetchone()
            if row is None:
                console.print(f"[red]no log entry #{n}[/] (ids: `wallexer log`)")
                return
            console.print(
                Panel(
                    json.dumps(_j(row["payload"]), indent=2, ensure_ascii=False),
                    title=f"log #{row['id']} · {row['kind']} · {row['ts']}",
                    expand=False,
                )
            )
            return
        rows = conn.execute("SELECT id, ts, kind, payload FROM events ORDER BY id").fetchall()
    finally:
        conn.close()
    t = Table(
        title=f"Logs ({len(rows)} entries) · `wallexer log <id>` for one in full",
        header_style="bold cyan",
    )
    for col in ("#", "Time (UTC)", "Kind", "Payload"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(str(r["id"]), r["ts"][:19], r["kind"], (r["payload"] or "")[:110])
    with console.pager(styles=True):
        console.print(t)


# ---------------------------------------------------------------------------------- reason


def decision_payload(conn: sqlite3.Connection, n: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (n,)).fetchone()
    if row is None:
        return None
    orders = conn.execute(
        "SELECT client_id, mode, symbol, side, type, price, quantity, status, executed_qty, executed_price, fee, error"
        " FROM orders WHERE decision_id=? ORDER BY id",
        (n,),
    ).fetchall()
    return {
        "decision_id": row["id"],
        "ts": row["ts"],
        "mode": row["mode"],
        "action": row["action"],
        "symbol": row["symbol"],
        "strategy": row["strategy"],
        "reason": row["reason"],
        "features": _j(row["features"]),
        "signal": _j(row["signal"]),
        "weights": _j(row["weights"]),
        "risk_checks": _j(row["risk_checks"]),
        "orders": [dict(o) for o in orders],
    }


def show_reason(settings: Settings, console: Console, n: int | None) -> None:
    conn = _conn(settings)
    try:
        if n is not None:
            p = decision_payload(conn, n)
            if p is None:
                console.print(f"[red]no decision #{n}[/] (ids: `wallexer reason`)")
                return
            _render_decision(p, console)
            return
        decisions = conn.execute(
            "SELECT id, ts, mode, action, symbol, strategy, reason FROM decisions ORDER BY id"
        ).fetchall()
        orders = conn.execute(
            "SELECT mode, symbol, side, status, executed_qty, executed_price FROM orders"
        ).fetchall()
        paper = conn.execute(
            "SELECT strategy, source, COUNT(*) n, SUM(CAST(net_tmn AS REAL)) net FROM paper_trades"
            " WHERE kind != 'conversion' GROUP BY strategy, source"
        ).fetchall()
    finally:
        conn.close()
    console.print(
        f"[bold]Decisions[/] {len(decisions)} · by action: {dict(Counter(d['action'] for d in decisions))}"
    )
    console.print(
        f"[bold]Orders[/] {len(orders)} · by mode: {dict(Counter(o['mode'] for o in orders))} · "
        f"filled: {sum(1 for o in orders if o['status'] == 'filled')}"
    )
    pt = Table(title="Algorithms used (paper trades)", header_style="bold cyan")
    for col in ("Strategy", "Source", "Trades", "Net TMN"):
        pt.add_column(col, justify="right")
    for r in paper:
        pt.add_row(r["strategy"], r["source"], str(r["n"]), f"{r['net'] or 0:,.0f}")
    console.print(pt)
    t = Table(
        title="Transactions / decisions · `wallexer reason <id>` for full detail",
        header_style="bold cyan",
    )
    for col in ("#", "Time", "Mode", "Action", "Market", "Strategy", "Reason"):
        t.add_column(col, overflow="fold")
    for d in decisions[-200:]:
        t.add_row(
            str(d["id"]),
            d["ts"][:19],
            d["mode"],
            d["action"],
            d["symbol"] or "",
            d["strategy"] or "",
            (d["reason"] or "")[:90],
        )
    console.print(t)
    if len(decisions) > 200:
        console.print(f"[dim](last 200 of {len(decisions)} shown; `wallexer export` has all)[/]")


def _render_decision(p: dict[str, Any], console: Console) -> None:
    console.print(
        Panel(
            f"[bold]#{p['decision_id']}[/] {p['action']} · {p['mode']} · {p['symbol'] or ''} {p['strategy'] or ''} · {p['ts']}\n"
            f"[bold]reason[/] {p['reason']}",
            title="Decision",
            expand=False,
        )
    )
    for key, title in (
        ("features", "Features"),
        ("signal", "Signal / model inputs and thresholds"),
        ("weights", "Strategy weights"),
        ("risk_checks", "Risk-gate checks / before → after"),
    ):
        if p.get(key):
            console.print(
                Panel(
                    json.dumps(p[key], indent=2, ensure_ascii=False, default=str),
                    title=title,
                    expand=False,
                )
            )
    if p["orders"]:
        t = Table(title="Resulting orders", header_style="bold cyan")
        for col in p["orders"][0]:
            t.add_column(col)
        for o in p["orders"]:
            t.add_row(*[str(v) for v in o.values()])
        console.print(t)
    else:
        console.print("[dim]no orders resulted from this decision[/]")


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

    conn = _conn(settings)
    try:
        if n is None:
            row = conn.execute("SELECT id FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                console.print("No decisions yet.")
                return
            n = row[0]
        p = decision_payload(conn, n)
        if p is None:
            console.print(f"[red]no decision #{n}[/]")
            return
        eq_row = conn.execute(
            "SELECT equity_tmn FROM equity_curve ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        equity = float(eq_row[0]) if eq_row else float(settings.cfg.account.expected_equity_tmn)
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
            f"[red]LLM explanation unavailable[/] ({res.error}). The static record: `wallexer reason {n}`."
        )
        return
    e: Explanation = res.parsed  # type: ignore[assignment]
    color = {"low": "red", "medium": "yellow", "high": "green"}[e.confidence]
    console.print(
        Panel(
            f"[bold]Summary[/]\n{e.summary}\n\n[bold]Why this action[/]\n{e.why_this_action}\n\n"
            f"[bold]Risk[/]\n{e.risk}\n\n[bold]Confidence[/] [{color}]{e.confidence}[/]",
            title=f"Explanation of decision #{n} ({res.source}, llm_call #{res.llm_call_id})",
            expand=False,
        )
    )
