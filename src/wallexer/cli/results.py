"""`rslt <backtest|shadowtest>`, `export`, `config guardrail`, `autoset guardrail`."""

from __future__ import annotations

import csv
import io
import json
import tomllib
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from rich.console import Console

from wallexer.config import DEFAULT_CONFIG, Settings
from wallexer.store.db import init_db, log_event

# ---------------------------------------------------------------------------------- rslt


def rslt(settings: Settings, console: Console, which: str, full: bool = False) -> None:
    """Local DB or remote API (cli/source.py); one renderer for both."""
    from wallexer.cli import source, views

    d = source.rslt(settings, which)
    if which == "backtest":
        views.render_rslt_backtest(d, console, full)
    else:
        views.render_rslt_shadow(d, console, full)


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
    from wallexer.cli.theme import table

    t = table(f"Export → {out}", [("Table", "text"), ("Rows", "num")])
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
    from wallexer.cli.theme import table

    cols = [("Name", "text"), ("Current", "num"), ("Min", "num"), ("Max", "num")]
    if suggested:
        cols += [("Suggested", "num"), ("Why", "text")]
    t = table("Guardrails (bounds are hard limits; edits apply live)", cols)
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
