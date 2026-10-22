"""wallexer CLI entry point (Typer). Commands print short errors, never tracebacks."""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable
from typing import Any

import typer
from rich.console import Console

from wallexer.config import ConfigError, load_settings
from wallexer.exchange.account import PreflightError
from wallexer.exchange.errors import WallexError
from wallexer.logging import setup_logging

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="wallexer",
    help="Automated spot-trading engine + CLI for Wallex (wallex.ir).",
    add_completion=False,
    pretty_exceptions_enable=False,
)


def friendly(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn expected failures into a one-line error + hint, exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (ConfigError, PreflightError, WallexError, FileNotFoundError) as e:
            err_console.print(f"[bold red]error:[/] {e}")
            err_console.print("[dim]hint: run `wallexer doctor` to check configuration.[/]")
            raise typer.Exit(1) from None
        except KeyboardInterrupt:
            err_console.print("[yellow]interrupted[/]")
            raise typer.Exit(130) from None
        except typer.Exit:
            raise
        except Exception as e:  # last resort: never show a raw traceback to the user
            if os.environ.get("WALLEXER_DEBUG"):
                raise
            err_console.print(f"[bold red]unexpected error:[/] {type(e).__name__}: {str(e)[:200]}")
            err_console.print("[dim]hint: re-run with WALLEXER_DEBUG=1 for a traceback.[/]")
            raise typer.Exit(1) from None

    return wrapper


@app.command()
@friendly
def markets(
    all_: bool = typer.Option(
        False, "--all", help="Show every allowed spot market, not just the ranked top N."
    ),
) -> None:
    """Allowed universe: volume, spread, fees, zero-fee and MARKET-order flags."""
    from wallexer.cli.markets import run

    asyncio.run(run(load_settings(), console, show_all=all_))


@app.command()
@friendly
def doctor(
    quick: bool = typer.Option(
        False, "--quick", help="Skip the rate-limit window measurement (~2 min)."
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the BoxAPI checks."),
) -> None:
    """Connectivity, keys, trading credit, fees, clock skew, rate limits, BoxAPI."""
    from wallexer.cli.doctor import run

    ok = asyncio.run(run(load_settings(), console, quick=quick, check_llm=not no_llm))
    if not ok:
        raise typer.Exit(1)


engine_app = typer.Typer(help="Engine daemon (long-running).", no_args_is_help=True)
app.add_typer(engine_app, name="engine")


@engine_app.command("run")
@friendly
def engine_run(
    mode: str = typer.Option("shadow", "--mode", help="shadow | trade"),
    market: list[str] = typer.Option(
        None, "--market", help="Restrict to these markets (repeatable)."
    ),
    replay: bool = typer.Option(
        False, "--replay", help="LLM answers from recorded llm_calls only."
    ),
) -> None:
    """Run the engine in the foreground (JSON logs to stdout). Used by hosting and by shadowtest/trade."""
    from wallexer.config import ConfigError, Mode
    from wallexer.engine.daemon import Engine
    from wallexer.logging import setup_logging

    try:
        m = Mode(mode)
    except ValueError:
        raise ConfigError(f"unknown mode {mode!r}: use shadow or trade") from None
    setup_logging(level=20, json=True)
    settings = load_settings()
    if replay:
        settings.cfg.llm.replay = True
    asyncio.run(Engine(settings, m, market or None).run())


@app.command()
@friendly
def shadowtest(
    markets: list[str] = typer.Argument(None, help="Markets (default: whole universe)."),
) -> None:
    """Start shadow mode (live data, simulated orders) as a detached engine."""
    from wallexer.cli.engine_cmds import engine_running, spawn, wait_ready
    from wallexer.config import Mode

    settings = load_settings()
    if engine_running(settings):
        console.print(
            "[yellow]Engine already running.[/] Use `wallexer status`, or `wallexer stop` first."
        )
        raise typer.Exit(1)
    pid = spawn(settings, Mode.shadow, [m.upper() for m in markets or []])
    if wait_ready(settings, console):
        console.print(
            f"[green]Shadow engine running[/] (pid {pid}). `wallexer status` to watch, `wallexer stop` to stop."
        )
    else:
        console.print(f"[red]Engine did not come up.[/] See {settings.cfg.engine.log_path}.")
        raise typer.Exit(1)


@app.command()
@friendly
def trade(
    markets: list[str] = typer.Argument(None, help="Markets (default: whole universe)."),
) -> None:
    """Start LIVE trading (real orders) after the human-in-the-loop prompt and confirmation."""
    from wallexer.cli.engine_cmds import engine_running, human_in_the_loop, spawn, wait_ready
    from wallexer.config import Mode

    settings = load_settings()
    if engine_running(settings):
        console.print("[yellow]An engine is already running.[/] Stop it first (`wallexer stop`).")
        raise typer.Exit(1)
    if not human_in_the_loop(settings, console):
        console.print("OK: review first. Run `wallexer shadowtest` and `wallexer status`.")
        raise typer.Exit(0)
    confirm = console.input("[bold red]This places REAL orders.[/] Type TRADE to start: ").strip()
    if confirm != "TRADE":
        console.print("Not confirmed. Nothing started.")
        raise typer.Exit(1)
    pid = spawn(settings, Mode.trade, [m.upper() for m in markets or []])
    if wait_ready(settings, console):
        console.print(f"[bold red]LIVE engine running[/] (pid {pid}).")
    else:
        console.print(f"[red]Engine did not come up.[/] See {settings.cfg.engine.log_path}.")
        raise typer.Exit(1)


@app.command()
@friendly
def status() -> None:
    """Engine status: mode, equity, drawdown, strategies (backtest + shadow), feed health."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, get_status, render_status

    st, live = get_status(load_settings())
    if st is None:
        console.print(NOT_RUNNING)
        raise typer.Exit(1)
    render_status(st, live, console)


@app.command()
@friendly
def stop(
    markets: list[str] = typer.Argument(None, help="Markets to stop (default: everything)."),
) -> None:
    """Stop all trading (engine exits) or only the given markets (their open orders are cancelled)."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, sock
    from wallexer.engine import control

    try:
        resp = control.send(
            sock(load_settings()), "stop", markets=[m.upper() for m in markets or []]
        )
    except control.EngineNotRunning:
        console.print(NOT_RUNNING)
        raise typer.Exit(1) from None
    console.print(resp)


@app.command()
@friendly
def flatten(
    markets: list[str] = typer.Argument(None, help="Markets to flatten (default: all balances)."),
) -> None:
    """Sell every non-TMN balance (incl. USDT) to TMN, or only the given markets' coins."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, sock
    from wallexer.engine import control

    try:
        resp = control.send(
            sock(load_settings()), "flatten", markets=[m.upper() for m in markets or []]
        )
    except control.EngineNotRunning:
        console.print(NOT_RUNNING)
        raise typer.Exit(1) from None
    console.print(resp)


history_app = typer.Typer(help="Candle history (30 days, incremental).", no_args_is_help=True)
app.add_typer(history_app, name="history")


@history_app.command("sync")
@friendly
def history_sync(
    days: int = typer.Option(30, "--days", help="How many days back to keep."),
) -> None:
    """Fetch missing 1-min / 60-min candles for the universe; rebuild 5/15-min from 1-min."""
    from wallexer.cli.backtest_cmds import run_history_sync

    asyncio.run(run_history_sync(load_settings(), console, days))


@app.command()
@friendly
def backtest(
    walk_forward: bool = typer.Option(
        False, "--walk-forward", help="Tune on the oldest 2/3 of history, judge on the newest 1/3."
    ),
) -> None:
    """Backtest on stored candles (30 days, incremental sync first)."""
    from wallexer.cli.backtest_cmds import run_walk_forward

    if not walk_forward:
        console.print("Only --walk-forward is implemented: `wallexer backtest --walk-forward`.")
        raise typer.Exit(1)
    asyncio.run(run_walk_forward(load_settings(), console))


research_app = typer.Typer(help="Research studies (read-only, no orders).", no_args_is_help=True)
app.add_typer(research_app, name="research")


@research_app.command("spread")
@friendly
def research_spread(
    collect: bool = typer.Option(
        False, "--collect", help="Collect books/trades (read-only) instead of analyzing."
    ),
    hours: float = typer.Option(4.0, "--hours", help="How long to collect."),
) -> None:
    """Spread-capture scan over all allowed markets: net edge after 2 maker fees, fills, drift."""
    from wallexer.cli.backtest_cmds import run_spread_scan
    from wallexer.logging import setup_logging

    if collect:
        setup_logging(level=20, json=True)
    asyncio.run(run_spread_scan(load_settings(), console, collect, hours))


@research_app.command("basis")
@friendly
def research_basis() -> None:
    """Triangle basis (X/TMN vs X/USDT x USDT/TMN) on stored simultaneous order books."""
    from wallexer.cli.backtest_cmds import run_basis_study

    asyncio.run(run_basis_study(load_settings(), console))


agent_app = typer.Typer(help="LLM agent.", no_args_is_help=True)
app.add_typer(agent_app, name="agent")


@agent_app.command("now")
@friendly
def agent_now() -> None:
    """Ask the running engine for an agent decision now (otherwise every 30 min / on risk events)."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, render_agent, sock
    from wallexer.engine import control

    try:
        resp = control.send(sock(load_settings()), "agent_now", timeout=120)
    except control.EngineNotRunning:
        console.print(NOT_RUNNING)
        raise typer.Exit(1) from None
    if not resp.get("ok"):
        console.print(f"[red]{resp.get('error')}[/]")
        raise typer.Exit(1)
    render_agent(resp["agent"] | {"applied": True}, console)


@agent_app.command("replay")
@friendly
def agent_replay(
    call_id: int = typer.Argument(..., help="llm_calls id of a recorded agent decision."),
) -> None:
    """Re-run a recorded agent prompt in replay mode; must reproduce the recorded decision offline."""
    from wallexer.cli.agent_cmds import replay_call

    if not asyncio.run(replay_call(load_settings(), console, call_id)):
        raise typer.Exit(1)


@app.command()
@friendly
def log(n: int = typer.Argument(None, help="Log entry id (default: all, paged).")) -> None:
    """All logs (paged), or one entry in full."""
    from wallexer.cli.logs import show_log

    show_log(load_settings(), console, n)


@app.command()
@friendly
def reason(n: int = typer.Argument(None, help="Decision id (default: overview).")) -> None:
    """Static explanation: all transactions/algorithms, or one decision's numbers in full."""
    from wallexer.cli.logs import show_reason

    show_reason(load_settings(), console, n)


@app.command("exp-reason")
@friendly
def exp_reason(n: int = typer.Argument(None, help="Decision id (default: latest).")) -> None:
    """LLM explanation of `reason n` (anonymized; summary, why, risk, confidence)."""
    from wallexer.cli.logs import exp_reason as run

    asyncio.run(run(load_settings(), console, n))


@app.command()
@friendly
def rslt(which: str = typer.Argument(..., help="backtest | shadowtest")) -> None:
    """Summary of the latest backtest or shadowtest run."""
    from wallexer.cli.results import rslt as run

    if which not in ("backtest", "shadowtest"):
        console.print("[red]usage:[/] wallexer rslt <backtest|shadowtest>")
        raise typer.Exit(2)
    run(load_settings(), console, which)


@app.command()
@friendly
def export() -> None:
    """Zip decisions, orders, fills, equity, allocations, llm_calls, reports, basis study, dust, config."""
    from wallexer.cli.results import export as run

    settings = load_settings()
    basis_md = asyncio.run(_basis_markdown(settings))
    run(settings, console, basis_md)


async def _basis_markdown(settings) -> str | None:
    from wallexer.exchange.client import WallexClient
    from wallexer.research.basis_study import BASES, load_cycles, markdown, study
    from wallexer.store.db import init_db

    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        cycles, skew = load_cycles(
            conn, ["USDTTMN"] + [f"{b}{q}" for b in BASES for q in ("TMN", "USDT")]
        )
    finally:
        conn.close()
    if not cycles:
        return None
    async with WallexClient(settings) as client:
        fees = await client.fees()
    ts = [min(s.ts for s in c.values()) for c in cycles]
    return markdown(study(cycles, fees), len(cycles), (max(ts) - min(ts)) / 3600, skew)


config_app = typer.Typer(help="Configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
autoset_app = typer.Typer(help="LLM-suggested settings (never auto-applied).", no_args_is_help=True)
app.add_typer(autoset_app, name="autoset")


def _reload_engine() -> None:
    from wallexer.cli.engine_cmds import sock
    from wallexer.engine import control

    try:
        resp = control.send(sock(load_settings()), "reload")
        console.print(f"engine: {resp}")
    except control.EngineNotRunning:
        console.print("[dim]engine not running: applies at next start[/]")


@config_app.command("guardrail")
@friendly
def config_guardrail() -> None:
    """Show guardrails with bounds; edit values interactively (validated); applied live."""
    from wallexer.cli.results import config_guardrail as run

    changes = run(load_settings(), console)
    if changes:
        console.print(f"[green]saved[/] {changes}")
        _reload_engine()
    else:
        console.print("No changes.")


@autoset_app.command("guardrail")
@friendly
def autoset_guardrail() -> None:
    """LLM suggests guardrail values from current market stats; you accept all, some or none."""
    from wallexer.cli.engine_cmds import get_status
    from wallexer.cli.results import autoset_guardrail as run

    settings = load_settings()
    st, _live = get_status(settings)
    stats = {
        "feed": (st or {}).get("feed"),
        "regime": (st or {}).get("regime"),
        "drawdown_pct": (st or {}).get("drawdown_pct"),
        "phase": (st or {}).get("phase"),
    }
    changes = asyncio.run(run(settings, console, stats))
    if changes:
        console.print(f"[green]accepted and saved[/] {changes}")
        _reload_engine()
    else:
        console.print("Nothing applied.")


@app.command()
@friendly
def change(
    src: str = typer.Argument(..., metavar="FROM", help="backtest | shadowtest | trade"),
    dst: str = typer.Argument(..., metavar="TO", help="backtest | shadowtest | trade"),
) -> None:
    """Switch modes (all 6 transitions); trade transitions ask for confirmation."""
    from wallexer.cli.change import change as run

    code = asyncio.run(run(load_settings(), console, src, dst))
    if code:
        raise typer.Exit(code)


@app.command()
@friendly
def watch() -> None:
    """Live dashboard (Textual). `q` / Quit closes the view only; the engine keeps running."""
    from wallexer.cli.watch import run_watch

    run_watch(load_settings())


def main() -> None:
    import sys

    setup_logging()
    if len(sys.argv) == 1:
        from wallexer.cli.repl import run_repl

        run_repl(app, console)
        return
    app()


if __name__ == "__main__":
    main()
