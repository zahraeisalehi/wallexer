"""Interactive shell: `wallexer` with no arguments. Every command works without the prefix.

- Banner: LOCAL or REMOTE (server URL), engine mode, equity / PnL, the event schedule.
- Tab completion for commands and subcommands, persistent history, `help`, typo suggestions.
- Ctrl-C clears the line (or interrupts the running command) and returns to the prompt; it never
  reaches an engine: engines run detached in their own session. `exit`, `quit` or Ctrl-D leave.
"""

from __future__ import annotations

import shlex
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel

from wallexer.config import ConfigError, data_home


def banner(console: Console) -> None:
    from wallexer.cli import source
    from wallexer.cli.engine_cmds import schedule_line
    from wallexer.cli.theme import mode_badge, pct, tmn
    from wallexer.config import load_settings
    from wallexer.risk.schedule import times

    try:
        settings = load_settings()
    except ConfigError as e:
        console.print(
            Panel(
                f"[err]configuration problem:[/] {e}\n`wallexer setup` / `wallexer doctor`",
                title="wallexer",
                expand=False,
            )
        )
        return
    origin = source.origin_label(settings)
    where = (
        f"[head]REMOTE[/] {origin.removeprefix('REMOTE ')}"
        if origin.startswith("REMOTE")
        else "[head]LOCAL[/]"
    )
    try:
        st, live, _o = source.status(settings)
    except (source.RemoteError, ConfigError) as e:
        st, live = None, False
        where += f" · [err]{e}[/]"
    g = settings.cfg.guardrails
    end = settings.secrets.event_end_utc
    sch = (st or {}).get("schedule") or times(
        end.timestamp() if end else None, g.flatten_no_entry_min * 60, g.flatten_force_min * 60
    )
    if st and live and not st.get("starting"):
        pnl = st.get("pnl_pct_vs_start")
        style = "err" if str(pnl).startswith("-") else "ok"
        engine = (
            f"{mode_badge(st.get('mode'))} run {st.get('run_id')} · equity {tmn(st.get('equity_tmn'))} TMN · "
            f"PnL [{style}]{pct(pnl, 3)}[/] · open orders {st.get('open_orders')}"
        )
    elif st and st.get("starting"):
        engine = f"{mode_badge(st.get('mode'))} starting…"
    else:
        engine = "[muted]engine not running[/] · start one with `shadowtest` (simulated) or `trade` (live)"
    keys = (
        ""
        if settings.has_private or origin != "LOCAL"
        else "\n[warn]no Wallex keys[/]: public commands only (markets, help) · `setup`"
    )
    console.print(
        Panel(
            f"{where} · {engine}\n[key]Schedule[/] {schedule_line(sch)}{keys}\n"
            "[muted]Tab completes · `help` lists commands · Ctrl-C clears the line · `exit` or Ctrl-D leaves[/]",
            title="[key]wallexer[/]",
            title_align="left",
            expand=False,
        )
    )


def run_repl(app: typer.Typer, console: Console) -> None:
    from wallexer.cli.app import run_args

    # Typer vendors click; duck-type the command group instead of importing click.
    group: Any = typer.main.get_command(app)
    names = sorted(group.commands)
    words = (
        names
        + [
            f"{g} {sub}"
            for g in names
            if hasattr(group.commands[g], "commands")
            for sub in group.commands[g].commands
        ]
        + ["exit", "quit"]
    )
    hist = data_home() / "data" / ".repl_history"
    hist.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        completer=WordCompleter(words, sentence=True), history=FileHistory(str(hist))
    )
    banner(console)
    while True:
        try:
            line = session.prompt("wallexer> ").strip()
        except KeyboardInterrupt:
            continue  # clears the line; Ctrl-D / exit leave
        except EOFError:
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        try:
            args = shlex.split(line)
        except ValueError as e:
            console.print(f"[err]error:[/] {e}")
            continue
        if args[0] == "wallexer":  # tolerate the prefix out of habit
            args = args[1:] or ["help"]
        try:
            run_args(args)
        except KeyboardInterrupt:
            console.print("[warn]interrupted[/] (a running engine is not affected)")
