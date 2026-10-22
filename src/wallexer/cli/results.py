"""`rslt <backtest|shadowtest>`, `export`, `config guardrail`, `autoset guardrail`."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tomllib
import zipfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from rich.console import Console
from rich.table import Table

from wallexer.config import DEFAULT_CONFIG, Settings
from wallexer.store.db import init_db, log_event

# ---------------------------------------------------------------------------------- rslt


def rslt(settings: Settings, console: Console, which: str) -> None:
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        if which == "backtest":
            _rslt_backtest(conn, console)
        else:
            _rslt_shadow(conn, settings, console)
    finally:
        conn.close()


def _rslt_backtest(conn: sqlite3.Connection, console: Console) -> None:
    run = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        console.print("No backtest yet: `wallexer backtest --walk-forward`.")
        return
    sel = json.loads(run["selected"])
    console.print(
        f"[bold]Walk-forward run #{run['id']}[/] ({run['ts'][:19]}) · grid {run['grid_size']} configs · "
        f"split {datetime.fromtimestamp(run['split_ts'], UTC):%Y-%m-%d %H:%M} · report {run['report_path']}"
    )
    t = Table(header_style="bold cyan")
    for col in ("Strategy", "Selected (IS)", "IS n / net", "OOS n / net", "Verdict"):
        t.add_column(col, overflow="fold")
    for name, s in sel.items():
        is_, oos = s.get("is") or {}, s.get("oos") or {}
        t.add_row(
            name,
            json.dumps(s.get("selected")) if s.get("selected") else "–",
            f"{is_.get('n', '–')} / {float(is_.get('net', 0)):,.0f}" if is_ else "–",
            f"{oos.get('n', '–')} / {float(oos.get('net', 0)):,.0f}" if oos else "–",
            ("[green]PASS[/] " if s["passes"] else "[red]FAIL[/] ") + s["reason"],
        )
    console.print(t)
    rows = conn.execute(
        "SELECT strategy, timeframe, segment, SUM(trades) n, SUM(CAST(net_tmn AS REAL)) net FROM backtest_results"
        " WHERE run_id=? AND market='*' GROUP BY strategy, timeframe, segment",
        (run["id"],),
    ).fetchall()
    agg = Table(
        title="All configs pooled, summed per strategy × timeframe", header_style="bold cyan"
    )
    for col in ("Strategy", "TF", "Segment", "Trades", "Net TMN"):
        agg.add_column(col, justify="right")
    for r in rows:
        agg.add_row(
            r["strategy"], f"{r['timeframe']}m", r["segment"], str(r["n"]), f"{r['net']:,.0f}"
        )
    console.print(agg)


def _max_dd(curve: list[float]) -> float:
    peak, worst = (curve[0] if curve else 0.0), 0.0
    for x in curve:
        peak = max(peak, x)
        worst = max(worst, (peak - x) / peak * 100 if peak else 0.0)
    return worst


def _rslt_shadow(conn: sqlite3.Connection, settings: Settings, console: Console) -> None:
    run = conn.execute(
        "SELECT run_id FROM equity_curve WHERE mode='shadow' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if run is None:
        console.print("No shadow run yet: `wallexer shadowtest`.")
        return
    run_id = run[0]
    curve = [
        float(r[0])
        for r in conn.execute(
            "SELECT equity_tmn FROM equity_curve WHERE run_id=? ORDER BY rowid", (run_id,)
        )
    ]
    start = float(settings.cfg.account.expected_equity_tmn)
    console.print(
        f"[bold]Shadow run {run_id}[/] · account return {(curve[-1] - start) / start * 100:+.3f}% · "
        f"max drawdown {_max_dd(curve):.3f}% · {len(curve)} equity points"
    )
    cap = float(settings.cfg.paper.capital_tmn)
    t = Table(title="Paper books (strategies + agent watch books)", header_style="bold cyan")
    for col in (
        "Book",
        "Source",
        "Return %",
        "Max DD %",
        "Trades",
        "Win rate",
        "Avg trade TMN",
        "Fees TMN",
    ):
        t.add_column(col, justify="right")
    books = conn.execute(
        "SELECT strategy, source, kind, CAST(net_tmn AS REAL) net, CAST(fees_tmn AS REAL) fees FROM paper_trades"
        " WHERE run_id=? AND source IN ('shadow','watch') ORDER BY id",
        (run_id,),
    ).fetchall()
    by: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in books:
        by.setdefault((r["strategy"], r["source"]), []).append(r)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT strategy FROM strategy_paper_pnl WHERE run_id=?", (run_id,)
        )
    }
    for name in sorted(names | {k[0] for k in by}):
        rows = by.get((name, "shadow"), []) or by.get((name, "watch"), [])
        trades = [r for r in rows if r["kind"] != "conversion"]
        net = sum(r["net"] for r in rows)
        eq, cum = [cap], cap
        for r in rows:
            cum += r["net"]
            eq.append(cum)
        t.add_row(
            name,
            rows[0]["source"] if rows else "shadow",
            f"{net / cap * 100:+.3f}",
            f"{_max_dd(eq):.3f}",
            str(len(trades)),
            f"{sum(r['net'] > 0 for r in trades) / len(trades) * 100:.0f}%" if trades else "–",
            f"{net / len(trades):,.0f}" if trades else "–",
            f"{sum(r['fees'] for r in rows):,.0f}",
        )
    console.print(t)
    allocs = conn.execute(
        "SELECT baseline, final, source FROM allocations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    changed = sum(
        1
        for a in allocs
        if {k: v for k, v in json.loads(a["baseline"]).items() if not k.startswith("_")}
        != json.loads(a["final"])
    )
    stances = Counter(
        (json.loads(r[0]) or {}).get("decision", {}).get("stance")
        for r in conn.execute(
            "SELECT signal FROM decisions WHERE run_id=? AND action='agent_decision'", (run_id,)
        )
    )
    fallbacks = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE run_id=? AND action='agent_fallback'", (run_id,)
    ).fetchone()[0]
    console.print(
        f"[bold]Agent vs baseline[/]: {len(allocs)} allocation cycles, sources {dict(Counter(a['source'] for a in allocs))}, "
        f"final ≠ baseline in {changed} · agent decisions by stance {dict(stances)} · fallbacks {fallbacks}"
    )


# ---------------------------------------------------------------------------------- export

TABLES = (
    "decisions",
    "orders",
    "fills",
    "equity_curve",
    "allocations",
    "llm_calls",
    "paper_trades",
    "backtest_runs",
    "backtest_results",
    "events",
)


def export(settings: Settings, console: Console, basis_md: str | None) -> str:
    cfg = settings.cfg
    conn = init_db(cfg.path(cfg.store.db_path))
    out = cfg.path(f"data/exports/wallexer-export-{datetime.now(UTC):%Y%m%dT%H%M%S}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for table in TABLES:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 (fixed table list)
                counts[table] = len(rows)
                cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(cols)
                w.writerows([tuple(r) for r in rows])
                z.writestr(f"tables/{table}.csv", buf.getvalue())
                z.writestr(
                    f"tables/{table}.json",
                    json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str),
                )
            dust = conn.execute(
                "SELECT ts, payload FROM events WHERE kind='flatten.dust_accepted'"
            ).fetchall()
            z.writestr(
                "dust.json",
                json.dumps([{"ts": d[0], **json.loads(d[1])} for d in dust], ensure_ascii=False),
            )
            for rep in sorted(cfg.path("data/reports").glob("*.md")):
                z.write(rep, f"reports/{rep.name}")
            if basis_md:
                z.writestr("reports/basis_study.md", basis_md)
            z.write(DEFAULT_CONFIG, "config/default.toml")
            ov = cfg.path(cfg.store.overrides_path)
            if ov.exists():
                z.write(ov, "config/overrides.toml")
            for name in ("not_allowed.txt", "unavailable_markets.txt"):
                p = cfg.path(f"data/{name}")
                if p.exists():
                    z.write(p, f"config/{name}")
    finally:
        conn.close()
    t = Table(title=f"Export → {out}", header_style="bold cyan")
    t.add_column("Table")
    t.add_column("Rows", justify="right")
    for k, v in counts.items():
        t.add_row(k, str(v))
    console.print(t)
    console.print(
        "[dim]Never included: .env, API keys. llm_calls contain only anonymized prompts.[/]"
    )
    return str(out)


# ---------------------------------------------------------------------------------- guardrails


def _write_overrides(settings: Settings, updates: dict[str, Any]) -> None:
    from wallexer.exchange.min_order import _dump_toml

    path = settings.cfg.path(settings.cfg.store.overrides_path)
    cur = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    cur.setdefault("guardrails", {}).update(
        {k: str(v) if isinstance(v, Decimal) else v for k, v in updates.items()}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Written by wallexer (learned values, guardrail edits). Overrides config/default.toml.\n"
        + _dump_toml(cur),
        encoding="utf-8",
    )


def _coerce(field: str, raw: str, current: Any) -> Any:
    from wallexer.risk.guardrails import GUARDRAIL_BOUNDS

    lo, hi = GUARDRAIL_BOUNDS[field]
    val = Decimal(raw) if isinstance(current, Decimal) else int(raw)
    if not (lo <= float(val) <= hi):
        raise ValueError(f"{field} must be within [{lo}, {hi}]")
    return val


def guardrail_table(
    settings: Settings, console: Console, suggested: dict[str, Any] | None = None
) -> None:
    from wallexer.risk.guardrails import GUARDRAIL_BOUNDS

    g = settings.cfg.guardrails.model_dump()
    t = Table(title="Guardrails", header_style="bold cyan")
    cols = ["Name", "Current", "Min", "Max"] + (["Suggested", "Why"] if suggested else [])
    for c in cols:
        t.add_column(c, overflow="fold")
    for k, (lo, hi) in GUARDRAIL_BOUNDS.items():
        row = [k, str(g[k]), str(lo), str(hi)]
        if suggested:
            s = suggested.get(k, {})
            row += [str(s.get("value", "–")), s.get("why", "")]
        t.add_row(*row)
    console.print(t)


def config_guardrail(settings: Settings, console: Console) -> dict[str, Any]:
    """Interactive edit with validation and bounds; saved to overrides.toml (caller reloads the engine)."""
    from wallexer.risk.guardrails import GUARDRAIL_BOUNDS

    guardrail_table(settings, console)
    g = settings.cfg.guardrails
    changes: dict[str, Any] = {}
    console.print("Enter a new value, or press Enter to keep the current one.")
    for k in GUARDRAIL_BOUNDS:
        cur = getattr(g, k)
        while True:
            raw = console.input(f"{k} [{cur}]: ").strip()
            if not raw:
                break
            try:
                val = _coerce(k, raw, cur)
            except (ValueError, ArithmeticError) as e:
                console.print(f"[red]{e}[/]")
                continue
            if val != cur:
                changes[k] = val
            break
    if changes:
        _write_overrides(settings, changes)
        conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
        log_event(conn, "config.guardrail_edit", {k: str(v) for k, v in changes.items()})
        conn.close()
    return changes


AUTOSET_SYSTEM = """You tune risk guardrails of a spot-crypto bot in a 2-day hackathon where max drawdown is scored and
losing nothing is the priority. You get each guardrail's current value and allowed [min, max], plus current market
stats (spreads, volatility, costs). Suggest a value for EVERY guardrail, within bounds, with a short numeric reason.
Prefer tighter risk unless the stats clearly justify otherwise."""


async def autoset_guardrail(
    settings: Settings, console: Console, market_stats: dict[str, Any]
) -> dict[str, Any]:
    from pydantic import BaseModel, ConfigDict, create_model

    from wallexer.agent.client import LlmClient
    from wallexer.risk.guardrails import GUARDRAIL_BOUNDS

    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "why"],
        "properties": {"value": {"type": "number"}, "why": {"type": "string"}},
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(GUARDRAIL_BOUNDS),
        "properties": {k: item for k in GUARDRAIL_BOUNDS},
    }

    class Item(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: float
        why: str

    Suggestions = create_model(
        "Suggestions",
        __config__=ConfigDict(extra="forbid"),
        **{k: (Item, ...) for k in GUARDRAIL_BOUNDS},
    )  # type: ignore[call-overload]
    g = settings.cfg.guardrails.model_dump(mode="json")
    payload = {
        "guardrails": {
            k: {"current": g[k], "min": lo, "max": hi} for k, (lo, hi) in GUARDRAIL_BOUNDS.items()
        },
        "market_stats": market_stats,
    }
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        client = LlmClient(settings, conn)
        res = await client.structured(
            purpose="autoset_guardrail",
            system=AUTOSET_SYSTEM,
            user=json.dumps(payload, sort_keys=True),
            schema=schema,
            model=Suggestions,
            schema_name="guardrail_suggestions",
        )
        await client.close()
        if res.parsed is None:
            console.print(f"[red]LLM suggestion unavailable[/] ({res.error}). Nothing changed.")
            return {}
        sug = res.parsed.model_dump()
        guardrail_table(settings, console, sug)
        ans = (
            console.input("Accept \\[a]ll, \\[s]ome (comma-separated names), or \\[n]one? \\[n] ")
            .strip()
            .lower()
        )
        if ans in ("a", "all"):
            names = list(GUARDRAIL_BOUNDS)
        elif ans in ("s", "some"):
            names = [
                x.strip()
                for x in console.input("names: ").split(",")
                if x.strip() in GUARDRAIL_BOUNDS
            ]
        else:
            names = []
        changes: dict[str, Any] = {}
        for k in names:
            try:
                changes[k] = _coerce(
                    k,
                    str(
                        sug[k]["value"]
                        if isinstance(getattr(settings.cfg.guardrails, k), Decimal)
                        else int(round(sug[k]["value"]))
                    ),
                    getattr(settings.cfg.guardrails, k),
                )
            except (ValueError, ArithmeticError) as e:
                console.print(f"[red]{k}: {e} (skipped)[/]")
        log_event(
            conn,
            "config.autoset_guardrail",
            {
                "llm_call_id": res.llm_call_id,
                "accepted": {k: str(v) for k, v in changes.items()},
                "answer": ans or "n",
            },
        )
        if changes:
            _write_overrides(settings, changes)
        return changes
    finally:
        conn.close()
