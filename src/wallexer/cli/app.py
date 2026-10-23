"""wallexer CLI entry point (Typer). Commands print short errors, never tracebacks.

`wallexer` alone opens the interactive shell (cli/repl.py). `--remote` makes the read-only views
(status, watch, log, reason, rslt, sum) read the server's API; commands that change state are
refused in remote mode before any request is made.
"""

from __future__ import annotations

import asyncio
import difflib
import functools
import os
import sys
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import typer

from wallexer.cli import source
from wallexer.cli.help import PANEL
from wallexer.cli.theme import make_console
from wallexer.config import DEMO_REFUSED, ConfigError, is_demo, load_settings
from wallexer.exchange.account import PreflightError
from wallexer.exchange.errors import WallexError
from wallexer.logging import setup_logging

console = make_console()
err_console = make_console(stderr=True)
# Set by the full-screen app (cli/tui.py): `watch` and a started engine open its split view.
WATCH_HOOK: dict[str, Callable[[], None] | None] = {"fn": None}

app = typer.Typer(
    name="wallexer",
    help="Automated spot-trading engine + CLI for Wallex (wallex.ir). `wallexer help` lists every command.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
)


def _cmd(name: str, **kw: Any) -> Callable[..., Any]:
    """app.command with its help panel (Trading, Testing, Results, Explain, Guardrails, System)."""
    return app.command(name, rich_help_panel=PANEL.get(name), **kw)


class InputError(ValueError):
    """A bad argument value: printed with the command's usage hint."""


def _err(msg: str, hint: str) -> None:
    err_console.print(f"[err]error:[/] {msg}")
    err_console.print(f"[muted]hint: {hint}[/]")


def friendly(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn expected failures into a one-line error + hint, exit code 1 (2 for bad input)."""
    name = fn.__name__.removesuffix("_cmd").replace("_", "-")

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from wallexer.cli.backtest_cmds import BacktestInputError
        from wallexer.cli.daily import DateError

        try:
            return fn(*args, **kwargs)
        except (InputError, BacktestInputError, DateError) as e:
            _err(str(e), f"`wallexer help {name.split('-')[0]}` shows the usage")
            raise typer.Exit(2) from None
        except source.RemoteReadOnly as e:
            _err(str(e), "run it on the server, or locally without --remote")
            raise typer.Exit(2) from None
        except source.RemoteError as e:
            _err(str(e), "check the server URL and view token: `wallexer setup`")
            raise typer.Exit(1) from None
        except ConfigError as e:
            _err(str(e), "`wallexer doctor` checks the configuration; `wallexer setup` sets keys")
            raise typer.Exit(1) from None
        except (PreflightError, WallexError) as e:
            _err(str(e), "`wallexer doctor` checks connectivity, keys and the trading credit")
            raise typer.Exit(1) from None
        except FileNotFoundError as e:
            _err(str(e), "`wallexer doctor` checks the configuration")
            raise typer.Exit(1) from None
        except KeyboardInterrupt:
            err_console.print("[warn]interrupted[/] (a running engine is not affected)")
            raise typer.Exit(130) from None
        except typer.Exit:
            raise
        except Exception as e:  # last resort: never show a raw traceback to the user
            if os.environ.get("WALLEXER_DEBUG"):
                raise
            _err(
                f"unexpected {type(e).__name__}: {str(e)[:200]}",
                "re-run with WALLEXER_DEBUG=1 for a traceback",
            )
            raise typer.Exit(1) from None

    return wrapper


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    remote: bool = typer.Option(
        False, "--remote", help="Read status/watch/log/reason/rslt/sum from the server (read-only)."
    ),
) -> None:
    if remote:
        source.FORCED_REMOTE["on"] = True
    cmd = ctx.invoked_subcommand
    if cmd is None:
        # No questions: a fresh install opens straight into the public demo (remote, read-only).
        # `wallexer setup` configures a local engine or a private server.
        notice = _probe_remote()
        from wallexer.cli.tui import can_run_app, run_app

        if can_run_app():
            run_app(notice)
        else:
            from wallexer.cli.repl import run_repl

            if notice:
                console.print(f"[warn]{notice}[/]")
            run_repl(app, console)
        return
    if cmd in DEMO_REFUSED and is_demo():
        _err(
            f"`{cmd}` is refused on this demo server (WALLEXER_DEMO=1): it runs shadow only",
            "status, watch, log, reason, rslt and sum work here",
        )
        raise typer.Exit(2)
    if cmd in source.WRITE_COMMANDS:
        try:
            on = source.remote_on(load_settings())
        except ConfigError:
            on = source.FORCED_REMOTE["on"]
        if on:
            try:
                demo = load_settings().cfg.remote.zero_config and not source.FORCED_REMOTE["on"]
            except ConfigError:
                demo = False
            _err(
                f"`{cmd}` is disabled in remote mode (read-only viewing)",
                "this is the public demo: `wallexer setup` sets up your own engine"
                if demo
                else "run it on the server, or locally without --remote",
            )
            raise typer.Exit(2)


def _probe_remote() -> str | None:
    """Zero-setup demo unreachable → local mode with a one-line notice (None if all is well)."""
    try:
        settings = load_settings()
        r = source.remote(settings)
    except ConfigError:
        return None
    if r is None or not r.demo:
        return None  # local, or a server the user configured: errors show in the header
    why = source.reachable(settings)
    if why is None:
        return None
    source.FORCED_LOCAL["on"] = True
    return f"demo server unreachable ({why}): local public commands only: markets, help, setup"


@_cmd("markets")
@friendly
def markets(
    all_: bool = typer.Option(
        False, "--all", help="Show every allowed spot market, not just the ranked top N."
    ),
) -> None:
    """Allowed universe: volume, spread, fees, zero-fee and MARKET-order flags."""
    from wallexer.cli.markets import run

    asyncio.run(run(load_settings(), console, show_all=all_))


@_cmd("doctor")
@friendly
def doctor(
    quick: bool = typer.Option(
        False, "--quick", help="Skip the rate-limit window measurement (~2 min)."
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the BoxAPI checks."),
    live_smoke: bool = typer.Option(
        False, "--live-smoke", help="Zero-cost LIVE order test (one unfillable order, then cancel)."
    ),
    discount_pct: float = typer.Option(
        20.0, "--discount-pct", help="Live smoke: limit price this % below the best bid."
    ),
) -> None:
    """Connectivity, keys, trading credit, fees, clock skew, rate limits, BoxAPI."""
    from wallexer.cli.doctor import run

    if live_smoke:
        from wallexer.cli.smoke import run_smoke

        source.guard_write(load_settings(), "doctor --live-smoke")
        if not asyncio.run(run_smoke(load_settings(), console, Decimal(str(discount_pct)))):
            raise typer.Exit(1)
        return

    ok = asyncio.run(run(load_settings(), console, quick=quick, check_llm=not no_llm))
    if not ok:
        raise typer.Exit(1)


engine_app = typer.Typer(help="Engine daemon (long-running).", no_args_is_help=True)
app.add_typer(engine_app, name="engine", rich_help_panel="System")


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


def _markets(markets: list[str] | None) -> list[str]:
    return sorted({m.strip().upper() for m in markets or [] for m in m.split(",") if m.strip()})


def _engine_mode(settings: Any) -> str | None:
    from wallexer.cli.engine_cmds import sock
    from wallexer.engine import control

    try:
        return control.send(sock(settings), "ping", timeout=3).get("mode")
    except (control.EngineNotRunning, OSError):
        return None


def _start_markets(settings: Any, mode: str, mkts: list[str]) -> None:
    from wallexer.cli.engine_cmds import sock
    from wallexer.engine import control

    resp = control.send(sock(settings), "start_markets", markets=mkts, mode=mode)
    if not resp.get("ok"):
        raise InputError(resp.get("error", "engine refused"))
    still = resp.get("paused") or []
    console.print(
        f"[ok]started[/] {', '.join(resp['started'])} on the running {mode} engine"
        + (f" · still stopped: {', '.join(still)}" if still else "")
    )


def _launch(settings: Any, mode: Any, mkts: list[str], no_watch: bool) -> None:
    from wallexer.cli.engine_cmds import spawn, wait_ready

    pid = spawn(settings, mode, mkts)
    if not wait_ready(settings, console):
        _err("the engine did not come up", f"see {settings.cfg.path(settings.cfg.engine.log_path)}")
        raise typer.Exit(1)
    label = "[live] LIVE engine [/]" if mode.value == "trade" else "[shadow] SHADOW engine [/]"
    scope = ", ".join(mkts) if mkts else "whole universe"
    console.print(f"{label} running (pid {pid}) · {scope} · `wallexer status`, `wallexer stop`")
    if not no_watch and WATCH_HOOK["fn"] is not None:
        WATCH_HOOK["fn"]()  # inside the full-screen app: open its split view
    elif not no_watch and sys.stdout.isatty():
        from wallexer.cli.watch import run_watch

        run_watch(settings)
        console.print("[muted]view closed · the engine keeps running[/]")


@_cmd("shadowtest")
@friendly
def shadowtest(
    markets: list[str] = typer.Argument(None, help="Markets (default: whole universe)."),
    no_watch: bool = typer.Option(False, "--no-watch", help="Do not open the live view."),
) -> None:
    """Start shadow mode (live data, simulated orders) and open the live view."""
    from wallexer.config import Mode

    settings = load_settings()
    mkts = _markets(markets)
    running = _engine_mode(settings)
    if running == "shadow" and mkts:
        _start_markets(settings, "shadow", mkts)
        return
    if running:
        _err(
            f"an engine is already running ({running})",
            "`wallexer status`; `wallexer stop` first, or `wallexer change` to switch modes",
        )
        raise typer.Exit(1)
    _launch(settings, Mode.shadow, mkts, no_watch)


@_cmd("trade")
@friendly
def trade(
    markets: list[str] = typer.Argument(None, help="Markets (default: whole universe)."),
    no_watch: bool = typer.Option(False, "--no-watch", help="Do not open the live view."),
) -> None:
    """Start LIVE trading (real orders) after the review prompt and a typed confirmation.

    On a running trade engine, `trade MARKET` re-starts only that market (after `stop MARKET`).
    """
    from wallexer.cli.engine_cmds import confirm_trade
    from wallexer.config import Mode

    settings = load_settings()
    mkts = _markets(markets)
    running = _engine_mode(settings)
    if running == "shadow":
        _err(
            "a SHADOW engine is running",
            "switch with `wallexer change shadowtest trade` (asks the same questions)",
        )
        raise typer.Exit(1)
    if running == "trade":
        if not mkts:
            _err(
                "the live engine is already running",
                "`wallexer status`; `wallexer trade MARKET` restarts one market",
            )
            raise typer.Exit(1)
        if confirm_trade(settings, console, f"trade {' '.join(mkts)} on the running engine"):
            _start_markets(settings, "trade", mkts)
            return
        raise typer.Exit(1)
    if not confirm_trade(settings, console, f"trade {' '.join(mkts) or 'whole universe'}"):
        raise typer.Exit(1)
    _launch(settings, Mode.trade, mkts, no_watch)


@_cmd("status")
@friendly
def status() -> None:
    """Mode, equity, PnL, drawdown, schedule, per-market state, strategies, agent, feed."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, render_status

    st, live, origin = source.status(load_settings())
    if st is None:
        console.print(NOT_RUNNING)
        raise typer.Exit(1)
    render_status(st, live, console, origin)


@_cmd("stop")
@friendly
def stop(
    markets: list[str] = typer.Argument(None, help="Markets to stop (default: everything)."),
) -> None:
    """Stop all trading (engine exits) or only the given markets (their open orders are cancelled)."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, sock
    from wallexer.engine import control

    mkts = _markets(markets)
    try:
        resp = control.send(sock(load_settings()), "stop", markets=mkts)
    except control.EngineNotRunning:
        console.print(NOT_RUNNING)
        raise typer.Exit(1) from None
    if mkts:
        console.print(
            f"[warn]stopped[/] {', '.join(mkts)}: no new entries, open orders cancelled · "
            f"stopped now: {', '.join(resp.get('paused') or [])} · restart one with "
            f"`wallexer trade {mkts[0]}` (or `shadowtest {mkts[0]}`)"
        )
    else:
        console.print(
            "[warn]engine stopping[/] · positions are kept; `wallexer flatten` sells them"
        )


@_cmd("flatten")
@friendly
def flatten(
    markets: list[str] = typer.Argument(None, help="Markets to flatten (default: all balances)."),
) -> None:
    """Sell every non-TMN balance (incl. USDT) to TMN, or only the given markets' coins."""
    from wallexer.cli.engine_cmds import NOT_RUNNING, sock
    from wallexer.engine import control

    settings = load_settings()
    mkts = _markets(markets)
    mode = _engine_mode(settings)
    if mode is None:
        console.print(NOT_RUNNING)
        raise typer.Exit(1)
    if mode == "trade":
        what = ", ".join(mkts) or "ALL non-TMN balances"
        ans = (
            console.input(f"[err]Sell {what} to TMN with REAL orders?[/] \\[y/N] ").strip().lower()
        )
        if ans not in ("y", "yes"):
            console.print("Cancelled.")
            raise typer.Exit(1)
    resp = control.send(sock(settings), "flatten", markets=mkts)
    console.print(f"[ok]flatten requested[/] ({resp.get('flatten')}) · progress: `wallexer status`")


history_app = typer.Typer(help="Candle history (30 days, incremental).", no_args_is_help=True)
app.add_typer(history_app, name="history", rich_help_panel="Testing")


@history_app.command("sync")
@friendly
def history_sync(
    days: int = typer.Option(30, "--days", help="How many days back to keep."),
) -> None:
    """Fetch missing 1-min / 60-min candles for the universe; rebuild 5/15-min from 1-min."""
    from wallexer.cli.backtest_cmds import run_history_sync

    asyncio.run(run_history_sync(load_settings(), console, days))


def _date(v: str | None, opt: str) -> Any:
    from datetime import UTC, datetime

    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise InputError(f"{opt} {v!r}: use YYYY-MM-DD (or YYYY-MM-DDTHH:MM), UTC") from None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


@_cmd("backtest")
@friendly
def backtest(
    date_from: str = typer.Option(
        None, "--from", help="Start (UTC, YYYY-MM-DD). Default: 30 days ago."
    ),
    date_to: str = typer.Option(None, "--to", help="End (UTC, YYYY-MM-DD). Default: now."),
    markets: list[str] = typer.Option(
        None, "--markets", help="Markets, comma-separated or repeated. Default: universe."
    ),
    timeframe: list[int] = typer.Option(
        None,
        "--timeframe",
        "--resolution",
        help="Minutes: 1, 5, 15, 60 (repeatable). Default: config.",
    ),
    walk_forward: bool = typer.Option(True, "--walk-forward", hidden=True),
    if_older_than: float = typer.Option(
        0, "--if-older-than", help="Skip if the latest default run is younger than this many hours."
    ),
) -> None:
    """Walk-forward backtest on stored candles (tune on the oldest 2/3, judge on the newest 1/3).

    No options = the config values: the run the promotion gate uses. Any option makes a custom
    run (shown by `rslt backtest`, never used for promotion).
    """
    from wallexer.cli.backtest_cmds import run_walk_forward

    del walk_forward  # always walk-forward; the flag is kept for the engine's re-run loop
    bad = [t for t in timeframe or [] if t not in (1, 5, 15, 60)]
    if bad:
        raise InputError(f"--timeframe {bad}: allowed 1, 5, 15, 60")
    lo, hi = _date(date_from, "--from"), _date(date_to, "--to")
    if lo and hi and lo >= hi:
        raise InputError("--from must be before --to")
    settings = load_settings()
    custom = bool(lo or hi or markets or timeframe)
    if if_older_than > 0 and not custom:
        from datetime import UTC, datetime

        from wallexer.backtest.report import latest_run
        from wallexer.store.db import init_db

        conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
        row = latest_run(conn)
        conn.close()
        if row is not None:
            age_h = (datetime.now(UTC) - datetime.fromisoformat(row["ts"])).total_seconds() / 3600
            if age_h < if_older_than:
                console.print(
                    f"Latest walk-forward run #{row['id']} is {age_h:.1f} h old: skipped."
                )
                return
    asyncio.run(
        run_walk_forward(
            settings,
            console,
            date_from=lo,
            date_to=hi,
            markets=_markets(markets) or None,
            timeframes=timeframe or None,
        )
    )


research_app = typer.Typer(help="Research studies (read-only, no orders).", no_args_is_help=True)
app.add_typer(research_app, name="research", rich_help_panel="Testing")


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
app.add_typer(agent_app, name="agent", rich_help_panel="Explain")


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
        console.print(f"[err]{resp.get('error')}[/]")
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


@_cmd("log")
@friendly
def log(n: int = typer.Argument(None, help="Log entry id (default: all, paged).")) -> None:
    """All logs (paged), or one entry in full."""
    from wallexer.cli.logs import show_log

    show_log(load_settings(), console, n)


@_cmd("reason")
@friendly
def reason(n: int = typer.Argument(None, help="Decision id (default: overview).")) -> None:
    """Static explanation: all transactions/algorithms, or one decision's numbers in full."""
    from wallexer.cli.logs import show_reason

    show_reason(load_settings(), console, n)


@_cmd("exp-reason")
@friendly
def exp_reason(n: int = typer.Argument(None, help="Decision id (default: latest).")) -> None:
    """LLM explanation of `reason n` (anonymized; summary, why, risk, confidence)."""
    from wallexer.cli.logs import exp_reason as run

    asyncio.run(run(load_settings(), console, n))


@_cmd("rslt")
@friendly
def rslt(which: str = typer.Argument(..., metavar="backtest|shadowtest")) -> None:
    """Latest run: return %, max drawdown, trades, win rate, avg trade, fees, per strategy, agent vs baseline."""
    from wallexer.cli.results import rslt as run

    which = {"shadow": "shadowtest", "bt": "backtest"}.get(which, which)
    if which not in ("backtest", "shadowtest"):
        raise InputError(f"unknown result {which!r}: use `rslt backtest` or `rslt shadowtest`")
    run(load_settings(), console, which)


@_cmd("sum")
@friendly
def sum_cmd(
    day: str = typer.Argument(
        None, metavar="[DATE]", help="YYYY-MM-DD, today or yesterday (UTC). Default: today so far."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Regenerate even if this day is cached."),
) -> None:
    """Daily market + agent summary: static facts, then an LLM explanation (anonymized, cached)."""
    from wallexer.cli.daily import run_sum

    code = asyncio.run(run_sum(load_settings(), console, day, refresh))
    if code:
        raise typer.Exit(code)


@_cmd("export")
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
app.add_typer(config_app, name="config", rich_help_panel="Guardrails")
autoset_app = typer.Typer(help="LLM-suggested settings (never auto-applied).", no_args_is_help=True)
app.add_typer(autoset_app, name="autoset", rich_help_panel="Guardrails")


def _reload_engine() -> None:
    from wallexer.cli.engine_cmds import sock
    from wallexer.engine import control

    try:
        resp = control.send(sock(load_settings()), "reload")
        console.print(f"engine: {resp}")
    except control.EngineNotRunning:
        console.print("[muted]engine not running: applies at next start[/]")


@config_app.command("guardrail")
@friendly
def config_guardrail() -> None:
    """Show guardrails with bounds; edit values interactively (validated); applied live."""
    from wallexer.cli.results import config_guardrail as run

    changes = run(load_settings(), console)
    if changes:
        console.print(f"[ok]saved[/] {changes}")
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
        console.print(f"[ok]accepted and saved[/] {changes}")
        _reload_engine()
    else:
        console.print("Nothing applied.")


@_cmd("change")
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


@_cmd("watch")
@friendly
def watch() -> None:
    """Live dashboard (Textual). `q` / Quit closes the view only; the engine keeps running."""
    from wallexer.cli.watch import run_watch

    settings = load_settings()
    if WATCH_HOOK["fn"] is not None:
        WATCH_HOOK["fn"]()  # inside the full-screen app: toggle its split view
        return
    if not sys.stdout.isatty():
        raise InputError("watch needs an interactive terminal; use `wallexer status` instead")
    run_watch(settings)


@_cmd("resume")
@friendly
def resume() -> None:
    """Lift a hard drawdown stop (human decision). Drawdown is measured again from current equity."""
    from wallexer.cli.engine_cmds import sock
    from wallexer.engine import control
    from wallexer.store.db import init_db, log_event

    settings = load_settings()
    if (
        console.input("Lift the hard stop and allow new entries again? Type RESUME: ").strip()
        != "RESUME"
    ):
        console.print("Not confirmed.")
        raise typer.Exit(1)
    try:
        console.print(control.send(sock(settings), "resume"))
    except control.EngineNotRunning:
        conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
        log_event(conn, "control.resume", {"by": "human", "engine": "not running"})
        conn.close()
        console.print("Recorded; the engine will start without the hard stop.")


@_cmd("health")
@friendly
def health() -> None:
    """Print the engine's /health JSON (handy inside the container: no quoting needed)."""
    import os
    import urllib.request

    port = os.environ.get("PORT", str(load_settings().cfg.engine.health_port))
    try:
        console.print(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read().decode()
        )
    except OSError as e:
        console.print(f"[err]health endpoint unreachable on :{port}[/] ({e})")
        raise typer.Exit(1) from None


@_cmd("setup")
@friendly
def setup(
    mode: str = typer.Option(None, "--mode", help="local | remote | skip (default: ask)."),
) -> None:
    """First-run wizard: local keys (Wallex, BoxAPI) or remote viewing (server URL + view token)."""
    from wallexer.cli.setup import run_setup

    choice = {"local": "1", "remote": "2", "skip": "3", None: None}.get(mode)
    if mode and choice is None:
        raise InputError(f"--mode {mode!r}: use local, remote or skip")
    if not run_setup(console, choice):
        raise typer.Exit(1)


@_cmd("help")
def help_cmd(command: list[str] = typer.Argument(None, help="A command (and subcommand).")) -> None:
    """All commands grouped, with one line and an example each; `help CMD` for its options."""
    from wallexer.cli.help import render_help

    if not command:
        render_help(console)
        return
    raise typer.Exit(run_args([*command, "--help"]))


def _usage_error(e: Any, group: Any) -> int:
    msg = e.format_message() if hasattr(e, "format_message") else str(e)
    ctx = getattr(e, "ctx", None)
    hint = "`wallexer help` lists every command"
    if msg.startswith("No such command"):
        bad = msg.split("'")[1] if "'" in msg else ""
        close = difflib.get_close_matches(bad, list(group.commands), n=1)
        msg = f"unknown command `{bad}`" + (f": did you mean `{close[0]}`?" if close else "")
    elif ctx is not None and ctx.command is not None and ctx.info_name not in (None, "wallexer"):
        hint = f"{ctx.get_usage().replace('Usage: ', 'usage: ')} · `wallexer help {ctx.info_name}`"
    _err(msg, hint)
    return 2


def run_args(args: list[str]) -> int:
    """Run one command line (shell or REPL) with short errors instead of Click's boxes."""
    group: Any = typer.main.get_command(app)
    try:
        rv = group.main(args=args, prog_name="wallexer", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except Exception as e:  # Click exceptions (vendored by Typer): duck-typed by name
        name = type(e).__name__
        if name == "Exit":
            return int(getattr(e, "exit_code", 0))
        if name == "Abort":
            err_console.print("[warn]aborted[/] (a running engine is not affected)")
            return 130
        if hasattr(e, "format_message"):
            return _usage_error(e, group)
        if os.environ.get("WALLEXER_DEBUG"):
            raise
        _err(f"unexpected {name}: {str(e)[:200]}", "re-run with WALLEXER_DEBUG=1 for a traceback")
        return 1


def main() -> None:
    import logging

    # Commands report problems themselves; library warnings (e.g. the known /depth/all 503 with
    # its per-market fallback) would only add noise. `engine run` sets up JSON logging itself.
    setup_logging(level=logging.ERROR)
    sys.exit(run_args(sys.argv[1:]))


if __name__ == "__main__":
    main()
