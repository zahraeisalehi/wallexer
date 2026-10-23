"""Data + rendering for the read-only views (log, reason, rslt, equity).

`*_data(conn, ...)` read the SQLite store and return plain JSON-able dicts. The engine's read-only
HTTP API serves exactly these dicts; `render_*` take them and print. So local mode and remote
mode (`wallexer --remote`) share one implementation and look identical.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel

from wallexer.cli.theme import pct, table, tmn, verdict


def _j(s: str | None) -> Any:
    try:
        return json.loads(s) if s else None
    except ValueError:
        return s


# ---------------------------------------------------------------------------------- log


def log_data(
    conn: sqlite3.Connection, n: int | None = None, after: int | None = None, limit: int = 500
) -> dict[str, Any]:
    if n is not None:
        row = conn.execute("SELECT id, ts, kind, payload FROM events WHERE id=?", (n,)).fetchone()
        return {"entry": None if row is None else {**dict(row), "payload": _j(row["payload"])}}
    rows = conn.execute(
        "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
        (after or 0, limit),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"total": total, "entries": [dict(r) for r in reversed(rows)]}


def render_log(data: dict[str, Any], console: Console, paged: bool = True) -> None:
    if "entry" in data:
        e = data["entry"]
        if e is None:
            console.print("[err]no such log entry[/] · list them with `wallexer log`")
            return
        console.print(
            Panel(
                json.dumps(e["payload"], indent=2, ensure_ascii=False),
                title=f"log #{e['id']} · {e['kind']} · {e['ts'][:19]} UTC",
                expand=False,
            )
        )
        return
    entries = data["entries"]
    shown = (
        f"{len(entries)} of {data['total']}" if data["total"] > len(entries) else str(len(entries))
    )
    t = table(
        f"Logs ({shown}) · `wallexer log <id>` shows one in full",
        [("#", "num"), ("Time (UTC)", "text"), ("Kind", "text"), ("Payload", "text")],
    )
    for e in entries:
        kind = e["kind"]
        style = (
            "err"
            if kind.startswith(("risk.", "order.rejected"))
            else "warn"
            if kind.startswith("feed.")
            else ""
        )
        t.add_row(
            str(e["id"]),
            e["ts"][:19],
            f"[{style}]{kind}[/]" if style else kind,
            (e["payload"] or "")[:110],
        )
    if paged:
        with console.pager(styles=True):
            console.print(t)
    else:
        console.print(t)


# ---------------------------------------------------------------------------------- reason


def reason_data(conn: sqlite3.Connection, n: int) -> dict[str, Any] | None:
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


def decisions_after(conn: sqlite3.Connection, after: int, limit: int = 200) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, ts, action, symbol, reason FROM decisions WHERE id > ? ORDER BY id LIMIT ?",
            (after, limit),
        )
    ]


def reason_overview_data(conn: sqlite3.Connection, limit: int = 200) -> dict[str, Any]:
    decisions = conn.execute(
        "SELECT id, ts, mode, action, symbol, strategy, reason FROM decisions ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    by_action = dict(
        conn.execute("SELECT action, COUNT(*) FROM decisions GROUP BY action").fetchall()
    )
    orders = conn.execute("SELECT mode, status FROM orders").fetchall()
    paper = conn.execute(
        "SELECT strategy, source, COUNT(*) n, SUM(CAST(net_tmn AS REAL)) net, SUM(CAST(fees_tmn AS REAL)) fees"
        " FROM paper_trades WHERE kind != 'conversion' GROUP BY strategy, source ORDER BY strategy, source"
    ).fetchall()
    return {
        "decisions_total": sum(by_action.values()),
        "by_action": by_action,
        "orders_total": len(orders),
        "orders_by_mode": dict(Counter(o["mode"] for o in orders)),
        "orders_filled": sum(1 for o in orders if (o["status"] or "").lower() == "filled"),
        "paper": [dict(p) for p in paper],
        "decisions": [dict(d) for d in reversed(decisions)],
    }


def render_reason_overview(d: dict[str, Any], console: Console) -> None:
    console.print(f"[key]Decisions[/] {d['decisions_total']} · by action {d['by_action']}")
    console.print(
        f"[key]Orders[/] {d['orders_total']} · by mode {d['orders_by_mode'] or '{}'} · filled {d['orders_filled']}"
    )
    t = table(
        "Algorithms used (paper trades)",
        [
            ("Strategy", "text"),
            ("Source", "text"),
            ("Trades", "num"),
            ("Net TMN", "num"),
            ("Fees TMN", "num"),
        ],
    )
    for p in d["paper"]:
        t.add_row(
            p["strategy"], p["source"], str(p["n"]), tmn(p["net"], signed=True), tmn(p["fees"])
        )
    console.print(t)
    # Narrow terminals (80 columns): drop Mode/Strategy and shorten the time so Reason stays readable.
    wide = console.width >= 110
    cols = [("#", "num"), ("Time (UTC)", "text")]
    cols += [("Mode", "text")] if wide else []
    cols += [("Action", "text"), ("Market", "text")]
    cols += [("Strategy", "text")] if wide else []
    t = table(
        "Transactions / decisions · `wallexer reason <id>` for full detail",
        [*cols, ("Reason", "text")],
    )
    for c in t.columns[:-1]:
        c.no_wrap = True
    for x in d["decisions"]:
        row = [str(x["id"]), x["ts"][:19] if wide else x["ts"][5:16].replace("T", " ")]
        row += [x["mode"]] if wide else []
        row += [x["action"], x["symbol"] or ""]
        row += [x["strategy"] or ""] if wide else []
        t.add_row(*row, (x["reason"] or "")[: 90 if wide else 160])
    console.print(t)
    if d["decisions_total"] > len(d["decisions"]):
        console.print(
            f"[muted]last {len(d['decisions'])} of {d['decisions_total']} shown; `wallexer export` has all[/]"
        )


def render_reason(p: dict[str, Any] | None, console: Console) -> None:
    if p is None:
        console.print("[err]no such decision[/] · list them with `wallexer reason`")
        return
    console.print(
        Panel(
            f"[key]#{p['decision_id']}[/] {p['action']} · {p['mode']} · {p['symbol'] or ''} {p['strategy'] or ''} · "
            f"{p['ts'][:19]} UTC\n[key]reason[/] {p['reason']}",
            title="Decision",
            expand=False,
        )
    )
    for key, title in (
        ("features", "Features"),
        ("signal", "Signal values, model inputs and thresholds"),
        ("weights", "Strategy weights (baseline / agent / final)"),
        ("risk_checks", "Risk-gate checks · before → after"),
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
        t = table(
            "Resulting orders",
            [
                (
                    c,
                    "num"
                    if c in ("price", "quantity", "executed_qty", "executed_price", "fee")
                    else "text",
                )
                for c in p["orders"][0]
            ],
        )
        for o in p["orders"]:
            t.add_row(*[("–" if v is None else str(v)) for v in o.values()])
        console.print(t)
    else:
        console.print("[muted]no orders resulted from this decision[/]")


# ---------------------------------------------------------------------------------- rslt


def _max_dd(curve: list[float]) -> float:
    peak, worst = (curve[0] if curve else 0.0), 0.0
    for x in curve:
        peak = max(peak, x)
        worst = max(worst, (peak - x) / peak * 100 if peak else 0.0)
    return worst


def rslt_backtest_data(conn: sqlite3.Connection, capital: float) -> dict[str, Any] | None:
    """Latest backtest of any kind; custom-window runs are labelled (never used for promotion)."""
    run = conn.execute(
        "SELECT * FROM backtest_runs WHERE kind LIKE 'walk_forward%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return None
    sel = json.loads(run["selected"])
    dd = {
        (r["strategy"], r["timeframe"], r["params"], r["segment"]): r["max_dd_pct"]
        for r in conn.execute(
            "SELECT strategy, timeframe, params, segment, max_dd_pct FROM backtest_results"
            " WHERE run_id=? AND market='*'",
            (run["id"],),
        )
    }
    pooled = [
        dict(r)
        for r in conn.execute(
            "SELECT strategy, timeframe, segment, COUNT(*) configs, SUM(trades) n, SUM(CAST(net_tmn AS REAL)) net"
            " FROM backtest_results WHERE run_id=? AND market='*'"
            " GROUP BY strategy, timeframe, segment ORDER BY strategy, timeframe, segment",
            (run["id"],),
        )
    ]
    strategies = []
    for name, s in sel.items():
        cfg, src = s.get("selected"), "selected"
        stats = s
        if cfg is None and s.get("best_is"):
            cfg, src, stats = s["best_is"]["config"], "best IS (not valid)", s["best_is"]
        row: dict[str, Any] = {
            "strategy": name,
            "config": cfg,
            "config_source": src if cfg else None,
            "passes": s["passes"],
            "reason": s["reason"],
        }
        for seg, key in (("IS", "is"), ("OOS", "oos")):
            st = stats.get(key) if cfg else None
            if not st:
                continue
            n, net, gl = int(st["n"]), float(st["net"]), float(st["gross_loss"])
            dd_key = (
                name,
                cfg["timeframe"],
                json.dumps(cfg["params"], sort_keys=True, separators=(",", ":")),
                seg,
            )
            row[key] = {
                "trades": n,
                "net": net,
                "return_pct": net / capital * 100,
                "win_rate": (int(st["wins"]) / n * 100) if n else None,
                "avg_trade": (net / n) if n else None,
                "pf": (float(st["gross_profit"]) / gl) if gl else None,
                "fees": float(st["fees"]) if st.get("fees") is not None else None,
                "max_dd_pct": _dd_lookup(dd, dd_key),
            }
        strategies.append(row)
    return {
        "run_id": run["id"],
        "kind": run["kind"],
        "ts": run["ts"],
        "grid_size": run["grid_size"],
        "split": datetime.fromtimestamp(run["split_ts"], UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "history": f"{datetime.fromtimestamp(run['history_from'], UTC):%Y-%m-%d %H:%M} → "
        f"{datetime.fromtimestamp(run['history_to'], UTC):%Y-%m-%d %H:%M} UTC",
        "report": run["report_path"],
        "strategies": strategies,
        "pooled": pooled,
    }


def _dd_lookup(dd: dict, key: tuple) -> Any:
    # params JSON spelling differs between writers; fall back to a normalised comparison
    if key in dd:
        return dd[key]
    name, tf, params, seg = key
    want = json.loads(params)
    for (n, t, p, sg), v in dd.items():
        if n == name and t == tf and sg == seg and json.loads(p) == want:
            return v
    return None


def render_rslt_backtest(d: dict[str, Any] | None, console: Console) -> None:
    if d is None:
        console.print("No backtest yet · run `wallexer backtest`.")
        return
    custom = d["kind"] != "walk_forward"
    label = "Custom backtest" if custom else "Walk-forward run"
    console.print(
        f"[key]{label} #{d['run_id']}[/] · history {d['history']} · split {d['split']} · "
        f"grid {d['grid_size']} configs · report {d['report']}"
    )
    if custom:
        console.print(
            "[warn]custom window/markets: shown for review, never used by the promotion gate[/]"
        )
    t = table(
        "Per strategy: config chosen in-sample, judged out-of-sample",
        [
            ("Strategy", "text"),
            ("Segment", "text"),
            ("Return", "num"),
            ("Max DD", "num"),
            ("Trades", "num"),
            ("Win rate", "num"),
            ("Avg trade TMN", "num"),
            ("PF", "num"),
            ("Fees TMN", "num"),
        ],
    )
    rows = 0
    for s in d["strategies"]:
        for key in ("is", "oos"):
            x = s.get(key)
            if not x:
                continue
            rows += 1
            t.add_row(
                s["strategy"] if key == "is" else "",
                key.upper(),
                pct(x["return_pct"], 3),
                pct(x["max_dd_pct"], 2, signed=False) if x.get("max_dd_pct") is not None else "–",
                str(x["trades"]),
                pct(x["win_rate"], 0, signed=False) if x["win_rate"] is not None else "–",
                tmn(x["avg_trade"], signed=True),
                f"{x['pf']:.2f}" if x["pf"] is not None else "–",
                tmn(x["fees"]),
            )
    if rows:
        console.print(t)
    else:
        console.print(
            "[muted]no per-strategy numbers in this run (runs before 2026-10-23 stored only "
            "verdicts); summed results below[/]"
        )
    for s in d["strategies"]:
        cfg = f" · {s['config_source']}: {json.dumps(s['config'])}" if s.get("config") else ""
        console.print(f"  {s['strategy']}: {verdict(s['passes'], s['reason'])}[muted]{cfg}[/]")
    t = table(
        "All configs, summed per strategy × timeframe",
        [
            ("Strategy", "text"),
            ("TF", "num"),
            ("Segment", "text"),
            ("Configs", "num"),
            ("Trades", "num"),
            ("Net TMN", "num"),
        ],
    )
    for r in d["pooled"]:
        t.add_row(
            r["strategy"],
            f"{r['timeframe']}m",
            r["segment"],
            str(r["configs"]),
            str(r["n"]),
            tmn(r["net"], signed=True),
        )
    console.print(t)


def rslt_shadow_data(
    conn: sqlite3.Connection, start: float, capital: float
) -> dict[str, Any] | None:
    run = conn.execute(
        "SELECT run_id, mode FROM equity_curve ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return None
    run_id = run["run_id"]
    curve = [
        float(r[0])
        for r in conn.execute(
            "SELECT equity_tmn FROM equity_curve WHERE run_id=? ORDER BY rowid", (run_id,)
        )
    ]
    rows = conn.execute(
        "SELECT strategy, source, kind, CAST(net_tmn AS REAL) net, CAST(fees_tmn AS REAL) fees FROM paper_trades"
        " WHERE run_id=? AND source IN ('shadow','watch') ORDER BY id",
        (run_id,),
    ).fetchall()
    by: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by.setdefault(r["strategy"], []).append(r)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT strategy FROM strategy_paper_pnl WHERE run_id=?", (run_id,)
        )
    }
    books = []
    for name in sorted(names | set(by)):
        rs = by.get(name, [])
        trades = [r for r in rs if r["kind"] != "conversion"]
        net = sum(r["net"] for r in rs)
        eq, cum = [capital], capital
        for r in rs:
            cum += r["net"]
            eq.append(cum)
        books.append(
            {
                "book": name,
                "source": rs[0]["source"] if rs else ("watch" if "@" in name else "shadow"),
                "return_pct": net / capital * 100,
                "max_dd_pct": _max_dd(eq),
                "trades": len(trades),
                "win_rate": (sum(r["net"] > 0 for r in trades) / len(trades) * 100)
                if trades
                else None,
                "avg_trade": (net / len(trades)) if trades else None,
                "fees": sum(r["fees"] for r in rs),
            }
        )
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
        (_j(r[0]) or {}).get("decision", {}).get("stance")
        for r in conn.execute(
            "SELECT signal FROM decisions WHERE run_id=? AND action='agent_decision'", (run_id,)
        )
    )
    fallbacks = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE run_id=? AND action='agent_fallback'", (run_id,)
    ).fetchone()[0]
    return {
        "run_id": run_id,
        "mode": run["mode"],
        "points": len(curve),
        "account_return_pct": ((curve[-1] - start) / start * 100) if curve else 0.0,
        "account_max_dd_pct": _max_dd(curve),
        "books": books,
        "agent": {
            "cycles": len(allocs),
            "sources": dict(Counter(a["source"] for a in allocs)),
            "final_differs_from_baseline": changed,
            "stances": {str(k): v for k, v in stances.items()},
            "fallbacks": fallbacks,
        },
    }


def render_rslt_shadow(d: dict[str, Any] | None, console: Console) -> None:
    if d is None:
        console.print("No shadow run yet · start one with `wallexer shadowtest`.")
        return
    console.print(
        f"[key]Run {d['run_id']}[/] ({d['mode']}) · account return {pct(d['account_return_pct'], 3)} · "
        f"max drawdown {pct(d['account_max_dd_pct'], 3, signed=False)} · {d['points']} equity points"
    )
    t = table(
        "Paper books (strategies + agent watch books)",
        [
            ("Book", "text"),
            ("Source", "text"),
            ("Return", "num"),
            ("Max DD", "num"),
            ("Trades", "num"),
            ("Win rate", "num"),
            ("Avg trade TMN", "num"),
            ("Fees TMN", "num"),
        ],
    )
    for b in d["books"]:
        t.add_row(
            b["book"],
            b["source"],
            pct(b["return_pct"], 3),
            pct(b["max_dd_pct"], 3, signed=False),
            str(b["trades"]),
            pct(b["win_rate"], 0, signed=False) if b["win_rate"] is not None else "–",
            tmn(b["avg_trade"], signed=True),
            tmn(b["fees"]),
        )
    console.print(t)
    a = d["agent"]
    console.print(
        f"[key]Agent vs baseline[/]: {a['cycles']} allocation cycles · sources {a['sources']} · final ≠ baseline "
        f"in {a['final_differs_from_baseline']} · agent stances {a['stances']} · fallbacks {a['fallbacks']}"
    )


# ---------------------------------------------------------------------------------- equity


def equity_data(conn: sqlite3.Connection, limit: int = 500) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT ts, mode, run_id, equity_tmn, tmn, exposure_pct, drawdown_pct FROM equity_curve ORDER BY rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"points": [dict(r) for r in reversed(rows)]}
