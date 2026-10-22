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

from wallexer.config import Mode, Settings
from wallexer.engine import control
from wallexer.store.db import init_db, log_event, now_iso

NOT_RUNNING = (
    "The engine is not running. Start it with `wallexer shadowtest` (shadow, detached) "
    "or `wallexer engine run --mode shadow` (foreground)."
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


def render_status(st: dict[str, Any], live: bool, console: Console) -> None:
    shadow = st.get("mode") == "shadow"
    banner = "SHADOW (simulated orders)" if shadow else "TRADE (LIVE ORDERS)"
    style = "bold black on yellow" if shadow else "bold white on red"
    age = (
        ""
        if live
        else f" · engine not running, last status from file ({_ago(st.get('written_at'))})"
    )
    console.print(
        Panel(
            f"[{style}] {banner} [/]  run {st.get('run_id')} · uptime {st.get('uptime_s')}s{age}",
            expand=False,
        )
    )

    t = Table(show_header=False, box=None)
    t.add_row("Equity", f"{int(st['equity_tmn']):,} TMN", "PnL vs 5M", f"{st['pnl_pct_vs_start']}%")
    t.add_row("Drawdown", f"{st['drawdown_pct']}%", "Exposure", f"{int(st['exposure_tmn']):,} TMN")
    t.add_row(
        "Phase",
        f"{st['phase']} (end {st.get('event_end_utc')})",
        "Open orders",
        str(st["open_orders"]),
    )
    t.add_row("Hard stop", str(st["hard_stopped"]), "Soft stop", str(st["soft_stop_active"]))
    usdt = st.get("usdt_inventory", {})
    t.add_row(
        "USDT inventory",
        f"{usdt.get('qty')} (≈{usdt.get('value_tmn')} TMN)",
        "Positions",
        ", ".join(f"{k} {v}" for k, v in st["positions"].items()) or "none",
    )
    console.print(t)
    if st.get("flat_by_design"):
        console.print("[bold cyan]flat by design: no strategy has proven edge yet[/]")

    wf = st.get("walk_forward") or {}
    age = wf.get("age_h")
    age_txt = (
        "no run"
        if age is None
        else f"run #{wf.get('run_id')}, {age}h old (max {wf.get('max_age_h')}h)"
    )
    s = Table(
        title=f"Strategies (promotion needs walk-forward OOS AND shadow · {age_txt})",
        header_style="bold cyan",
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
        color = "green" if x["promoted"] else ("magenta" if x["paper_only"] else "white")
        s.add_row(
            x["name"],
            f"[{color}]{status}[/]",
            f"{x.get('timeframe')}m",
            x["weight"],
            f"{bt.get('n')} / {bt.get('net_tmn')} / {bt.get('pf')}" if bt else "– (no selection)",
            f"{sh.get('n')} / {sh.get('net_tmn')} / {sh.get('pf')}",
            x["paper_equity"],
            ", ".join(x["open_paper_positions"] + [f"{p}(maker)" for p in x["pending_maker"]])
            or "–",
            x["reason"],
        )
    console.print(s)

    render_agent(st.get("agent") or {}, console)

    fd = Table(
        title=f"Feed ({st.get('depth_source')}, every {st.get('depth_interval_s')}s, rate-limit remaining {st.get('ratelimit_remaining')})",
        header_style="bold cyan",
    )
    for col in ("Market", "Spread bps", "Taker", "Round trip bps", "Book"):
        fd.add_column(col)
    for sym, v in st.get("feed", {}).items():
        fd.add_row(
            sym,
            v["spread_bps"],
            v["taker"],
            v["rt_bps"],
            f"[red]{v['issue']}[/]" if v["issue"] else "ok",
        )
    console.print(fd)
    rg = st.get("regime") or {}
    console.print(
        f"[dim]regime (percentiles vs 30d history): {rg} · allocation: {st.get('allocation_source')} · errors: {st.get('errors')}\n"
        f"walk-forward: grid {wf.get('grid_size')}, split {wf.get('split')}, report {wf.get('report')} · package: {st.get('package')}\n"
        f"events: {st.get('events')}\npreflight: {st.get('preflight')}[/]"
    )
    if st.get("dust"):
        console.print(f"[dim]dust accepted: {st['dust']}[/]")


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


def human_in_the_loop(settings: Settings, console: Console) -> bool:
    """cli-spec: before trade, always ask about backtest + shadow; show latest results; log answer."""
    st, live = get_status(settings)
    if st:
        console.print("[bold]Latest results:[/]")
        render_status(st, live, console)
    else:
        console.print("[yellow]No backtest/shadow results found yet.[/]")
    ans = console.input("Run/review backtest and shadowtest first? \\[Y/n] ").strip().lower()
    review_first = ans in ("", "y", "yes")
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    log_event(
        conn,
        "cli.trade_hitl_prompt",
        {"answer": ans or "Y(default)", "review_first": review_first, "ts": now_iso()},
    )
    conn.close()
    return not review_first
