"""`change <from> <to>`: the 6 transitions between backtest, shadowtest and trade (cli-spec)."""

from __future__ import annotations

import time

from rich.console import Console

from wallexer.config import Mode, Settings
from wallexer.engine import control

MODES = ("backtest", "shadowtest", "trade")


def _running_mode(settings: Settings) -> str | None:
    from wallexer.cli.engine_cmds import sock

    try:
        resp = control.send(sock(settings), "ping", timeout=5)
    except (control.EngineNotRunning, OSError):
        return None
    return {"shadow": "shadowtest", "trade": "trade"}.get(resp.get("mode", ""))


def _stop_engine(settings: Settings, console: Console, flatten_first: bool) -> bool:
    from wallexer.cli.engine_cmds import engine_running, get_status, sock

    if flatten_first:
        control.send(sock(settings), "flatten", markets=[])
        with console.status("Flattening all non-TMN balances to TMN…"):
            for _ in range(60):
                st, _live = get_status(settings)
                if st and not st.get("positions") and not st.get("open_orders"):
                    break
                time.sleep(3)
    control.send(sock(settings), "stop", markets=[])
    with console.status("Stopping engine…"):
        for _ in range(40):
            if not engine_running(settings):
                return True
            time.sleep(1)
    console.print("[red]Engine did not stop in time.[/]")
    return False


def _ask_keep_or_flatten(console: Console) -> bool:
    ans = (
        console.input("Live positions: \\[f]latten to TMN (default) or \\[k]eep? \\[f] ")
        .strip()
        .lower()
    )
    return ans not in ("k", "keep")


async def change(settings: Settings, console: Console, src: str, dst: str) -> int:
    from wallexer.cli.backtest_cmds import run_walk_forward
    from wallexer.cli.engine_cmds import human_in_the_loop, spawn, wait_ready

    if src not in MODES or dst not in MODES or src == dst:
        console.print(f"[red]usage:[/] wallexer change <from> <to> with from ≠ to in {MODES}")
        return 2
    running = _running_mode(settings)
    if src in ("shadowtest", "trade") and running != src:
        console.print(f"[red]Engine is not running in {src}[/] (running: {running or 'nothing'}).")
        return 1
    if src == "backtest" and running:
        console.print(
            f"[red]An engine is running in {running}.[/] Use `wallexer change {running} {dst}`."
        )
        return 1

    # Trade target: human-in-the-loop prompt and explicit confirmation BEFORE anything is stopped.
    if dst == "trade":
        if src == "shadowtest":
            ok = console.input(
                "Switch from SHADOW to LIVE trading? Virtual positions are NOT inherited; "
                "live starts from real balances. \\[y/N] "
            ).strip().lower() in ("y", "yes")
            if not ok:
                console.print("Cancelled.")
                return 1
        if not human_in_the_loop(settings, console):
            console.print("OK: review first. Nothing changed.")
            return 0
        if (
            console.input("[bold red]This places REAL orders.[/] Type TRADE to start: ").strip()
            != "TRADE"
        ):
            console.print("Not confirmed. Nothing changed.")
            return 1

    # 1) leave the current mode (trade asks keep/flatten, default flatten)
    if src in ("shadowtest", "trade"):
        flatten = _ask_keep_or_flatten(console) if src == "trade" else False
        if not _stop_engine(settings, console, flatten):
            return 1

    # 2) enter the target mode
    if dst == "backtest":
        await run_walk_forward(settings, console)
        return 0
    mode = Mode.trade if dst == "trade" else Mode.shadow
    pid = spawn(settings, mode, [])
    if wait_ready(settings, console):
        console.print(
            f"[green]{dst} running[/] (pid {pid}), using the latest walk-forward selection."
        )
        return 0
    console.print(f"[red]Engine did not come up.[/] See {settings.cfg.engine.log_path}.")
    return 1
