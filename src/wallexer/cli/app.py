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


def main() -> None:
    setup_logging()
    app()


if __name__ == "__main__":
    main()
