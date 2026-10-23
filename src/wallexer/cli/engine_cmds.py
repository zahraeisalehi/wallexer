"""Engine control commands: shadowtest, trade, status, stop, flatten, engine run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from wallexer.cli.theme import bps, mode_badge, pct, table, tmn
from wallexer.config import Mode, Settings
from wallexer.engine import control
from wallexer.store.db import init_db, log_event, now_iso

NOT_RUNNING = (
    "[warn]The engine is not running.[/] Start it with `wallexer shadowtest` (simulated orders) "
    "or `wallexer trade` (live). Watching the server instead? `wallexer --remote status`."
)


def sock(settings: Settings) -> Path:
    return settings.cfg.path(settings.cfg.engine.socket_path)


def engine_running(settings: Settings) -> bool:
    try:
        return bool(control.send(sock(settings), "ping", timeout=3).get("ok"))
    except (control.EngineNotRunning, OSError):
        return False


def spawn(settings: Settings, mode: Mode, markets: list[str]) -> int:
    """Start `engine run` detached (own session), logging to data/logs/engine.log."""
    log_path = settings.cfg.path(settings.cfg.engine.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "wallexer.cli.app", "engine", "run", "--mode", mode.value]
    for m in markets:
        cmd += ["--market", m]
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(settings.cfg.path(".")),
            env=os.environ.copy(),
        )
    return proc.pid


def wait_ready(settings: Settings, console: Console, timeout_s: float = 240) -> bool:
    """Startup includes the incremental history sync and regime calibration."""
    deadline = time.time() + timeout_s
    with console.status("Engine starting (preflight, universe, history sync)…"):
        while time.time() < deadline:
            if engine_running(settings):
                return True
            time.sleep(2)
    return False


def get_status(settings: Settings) -> tuple[dict[str, Any] | None, bool]:
    """(snapshot, live). Falls back to the last status file when the engine is down."""
    try:
        resp = control.send(sock(settings), "status", timeout=10)
        if resp.get("ok"):
            return resp["status"], True
    except (control.EngineNotRunning, OSError):
        pass
    path = settings.cfg.path("data/engine_status.json")
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8")), False
        except ValueError:
            return None, False
    return None, False


def render_status(st: dict[str, Any], live: bool, console: Console, origin: str = "LOCAL") -> None:
    if st.get("starting"):
        console.print(
            Panel(
                f"{mode_badge(st.get('mode'))}  engine starting (preflight, history sync)… · {origin}",
                expand=False,
            )
        )
        return
    shadow = st.get("mode") == "shadow"
    banner = (
        "[shadow] SHADOW · simulated orders [/]" if shadow else "[live] TRADE · LIVE ORDERS [/]"
    )
    age = (
        ""
        if live
        else f" · [warn]engine not running, last status from file ({_ago(st.get('written_at'))})[/]"
    )
    where = "" if origin == "LOCAL" else f" · [head]REMOTE[/] {origin}"
    console.print(
        Panel(
            f"{banner}  run {st.get('run_id')} · uptime {_uptime(st.get('uptime_s'))}{where}{age}",
            expand=False,
        )
    )

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="muted")
    t.add_column(justify="right")
    t.add_column(style="muted")
    t.add_column(justify="right")
    pnl = st["pnl_pct_vs_start"]
    pnl_style = "ok" if not str(pnl).startswith("-") else "err"
    t.add_row(
        "Equity", f"{tmn(st['equity_tmn'])} TMN", "PnL vs 5M", f"[{pnl_style}]{pct(pnl, 3)}[/]"
    )
    t.add_row(
        "Drawdown",
        pct(st["drawdown_pct"], 3, signed=False),
        "Exposure",
        f"{tmn(st['exposure_tmn'])} TMN",
    )
    t.add_row(
        "Phase",
        f"{st['phase']}",
        "Open orders",
        str(st["open_orders"]),
    )
    t.add_row(
        "Hard stop",
        "[err]ACTIVE[/]" if st["hard_stopped"] else "[ok]off[/]",
        "Soft stop",
        "[warn]active[/]" if st["soft_stop_active"] else "[ok]off[/]",
    )
    usdt = st.get("usdt_inventory", {})
    t.add_row(
        "USDT inventory",
        f"{usdt.get('qty')} (≈{usdt.get('value_tmn')} TMN)",
        "Positions",
        ", ".join(f"{k} {v}" for k, v in st["positions"].items()) or "none",
    )
    console.print(t)
    sch = st.get("schedule") or {}
    if sch.get("end"):
        console.print(f"[key]Schedule[/]: {schedule_line(sch)}")
    else:
        console.print("[warn]Schedule: EVENT_END_UTC not set: no entry stop, no auto-flatten[/]")
    if st.get("flat_by_design"):
        console.print("[head]flat by design: no strategy has proven edge yet[/]")
    render_market_states(st.get("market_states") or {}, console)

    wf = st.get("walk_forward") or {}
    age = wf.get("age_h")
    age_txt = (
        "no run"
        if age is None
        else f"run #{wf.get('run_id')}, {age}h old (max {wf.get('max_age_h')}h)"
    )
    s = Table(
        title=f"Strategies (promotion needs walk-forward OOS AND shadow · {age_txt})",
        header_style="head",
        title_justify="left",
    )
    for col in (
        "Strategy",
        "Status",
        "TF",
        "Weight",
        "OOS n / net / PF",
        "Shadow n / net / PF",
        "Paper equity",
        "Open",
        "Why",
    ):
        s.add_column(col, overflow="fold")
    for x in st["strategies"]:
        bt, sh = x.get("oos", {}), x.get("shadow", {})
        status = x["status"]
        color = "ok" if x["promoted"] else "muted"
        s.add_row(
            x["name"],
            f"[{color}]{status.split(':')[0]}[/]",
            f"{x.get('timeframe')}m",
            x["weight"],
            f"{bt.get('n')} / {bt.get('net_tmn')} / {bt.get('pf')}" if bt else "– (no selection)",
            f"{sh.get('n')} / {sh.get('net_tmn')} / {sh.get('pf')}",
            tmn(x["paper_equity"]),
            ", ".join(x["open_paper_positions"] + [f"{p}(maker)" for p in x["pending_maker"]])
            or "–",
            x["reason"],
        )
    console.print(s)

    render_agent(st.get("agent") or {}, console)

    fd = table(
        f"Feed ({st.get('depth_source')}, every {st.get('depth_interval_s')}s, rate-limit remaining {st.get('ratelimit_remaining')})",
        [
            ("Market", "text"),
            ("Spread bps", "num"),
            ("Taker", "num"),
            ("Round trip bps", "num"),
            ("Book", "text"),
        ],
    )
    for sym, v in st.get("feed", {}).items():
        fd.add_row(
            sym,
            bps(v["spread_bps"]),
            v["taker"],
            bps(v["rt_bps"]),
            f"[warn]{v['issue']}[/]" if v["issue"] else "[ok]ok[/]",
        )
    console.print(fd)
    rg = st.get("regime") or {}
    console.print(
        f"[muted]regime (percentiles vs 30d history): {rg} · allocation: {st.get('allocation_source')} · errors: {st.get('errors')}\n"
        f"walk-forward: grid {wf.get('grid_size')}, split {wf.get('split')}, report {wf.get('report')} · package: {st.get('package')}\n"
        f"events: {st.get('events')}\npreflight: {st.get('preflight')}[/]"
    )
    if st.get("dust"):
        console.print(f"[muted]dust accepted: {st['dust']}[/]")


def render_agent(ag: dict[str, Any], console: Console) -> None:
    if not ag or not ag.get("source"):
        console.print(
            "[dim]Agent: no decision yet (first cycle ~90 s after start, then every 30 min / on risk events).[/]"
        )
        return
    src = ag.get("source")
    color = {"llm": "green", "replay": "cyan", "fallback": "red"}.get(src, "white")
    lines = [
        f"[bold]stance[/] {ag.get('stance')} · source [{color}]{src}[/] · trigger {ag.get('trigger')} · "
        f"{ag.get('age_min')} min ago · applied {ag.get('applied')} · llm_call #{ag.get('llm_call_id')}",
        f"[bold]rationale[/] {ag.get('rationale')}",
        f"risk multiplier: agent {ag.get('risk_multiplier')} → effective {ag.get('effective_risk_multiplier')} · "
        f"paused {ag.get('pause_markets') or []} · active {ag.get('active_markets') or 'all'} · "
        f"blocked for entries {ag.get('blocked_markets') or []}",
    ]
    base, final, raw = (
        ag.get("baseline") or {},
        ag.get("final") or {},
        ag.get("agent_weights_raw") or {},
    )
    if base or final:
        diffs = ", ".join(
            f"{k}: baseline {base.get(k, '0')} / agent {raw.get(k, '–')} → final {final.get(k, '0')}"
            for k in sorted(set(base) | set(final))
        )
        lines.append(f"weights: {diffs}")
    if ag.get("watch_requests"):
        lines.append(
            "watch: "
            + "; ".join(
                f"{w['action']} {w['strategy']}@{w['timeframe']}m {w['market']} ({w['why']})"
                for w in ag["watch_requests"]
            )
        )
    if ag.get("notes"):
        lines.append(f"[dim]clamped: {ag['notes']}[/]")
    console.print(Panel("\n".join(lines), title="LLM agent", expand=False))


def _ago(ts: Any) -> str:
    try:
        return f"{int(time.time() - float(ts))}s ago"
    except (TypeError, ValueError):
        return "unknown"


def schedule_line(sch: dict[str, Any]) -> str:
    if not sch or not sch.get("end"):
        return "[warn]EVENT_END_UTC not set[/]"
    return f"entries stop {sch['entries_stop']} · flatten {sch['flatten_from']} · end {sch['end']}"


def _uptime(s: Any) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "–"
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


STATE_STYLE = {
    "active": "ok",
    "stopped (human)": "warn",
    "paused (agent)": "warn",
    "not active (agent)": "muted",
}


def render_market_states(states: dict[str, str], console: Console) -> None:
    """Per-market scope: each market can be stopped/started on its own (`stop m`, `trade m`)."""
    if not states:
        return
    by: dict[str, list[str]] = {}
    for sym, state in states.items():
        by.setdefault(state, []).append(sym)
    parts = [
        f"[{STATE_STYLE.get(state, 'err')}]{state}[/] {', '.join(syms)}"
        for state, syms in by.items()
    ]
    console.print("[key]Markets[/]: " + " · ".join(parts))


def _log(settings: Settings, kind: str, payload: dict[str, Any]) -> None:
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        log_event(conn, kind, {**payload, "ts": now_iso()})
    finally:
        conn.close()


def show_latest_results(settings: Settings, console: Console) -> None:
    """The latest backtest verdicts and shadow summary, shown before every trade start."""
    from wallexer.cli import source, views

    bt = source.rslt(settings, "backtest")
    sh = source.rslt(settings, "shadowtest")
    console.rule("[head]Latest backtest[/]", align="left")
    views.render_rslt_backtest(bt, console)
    console.rule("[head]Latest shadowtest[/]", align="left")
    views.render_rslt_shadow(sh, console)
    console.rule()


def confirm_trade(settings: Settings, console: Console, context: str) -> bool:
    """Human in the loop before ANY live start (cli-spec): always ask about backtest + shadowtest,
    show the latest results, then require typing TRADE. Both answers are logged. True = go."""
    show_latest_results(settings, console)
    ans = console.input("Run/review backtest and shadowtest first? \\[Y/n] ").strip().lower()
    review_first = ans not in ("n", "no")
    _log(
        settings,
        "cli.trade_hitl_prompt",
        {"context": context, "answer": ans or "Y(default)", "review_first": review_first},
    )
    if review_first:
        console.print(
            "OK: review first. Nothing started. `wallexer backtest`, `wallexer shadowtest`,"
            " `wallexer rslt shadowtest`."
        )
        return False
    typed = console.input(
        f"[err]This places REAL orders ({context}).[/] Type TRADE to confirm: "
    ).strip()
    confirmed = typed == "TRADE"
    _log(settings, "cli.trade_confirm", {"context": context, "confirmed": confirmed})
    if not confirmed:
        console.print("Not confirmed. Nothing started.")
    return confirmed
