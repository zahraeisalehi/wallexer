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
    no_args_is_help=True,
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
    asyncio.run(Engine(load_settings(), m, market or None).run())


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


def main() -> None:
    setup_logging()
    app()


if __name__ == "__main__":
    main()
