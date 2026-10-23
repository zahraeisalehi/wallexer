"""`wallexer help`: every command, grouped, with one line and one example each."""

from __future__ import annotations

from rich.console import Console

from wallexer.cli.theme import table

GROUPS: dict[str, list[tuple[str, str, str]]] = {
    "Trading": [
        (
            "shadowtest [MARKET...]",
            "start shadow mode (live data, simulated orders) + live view",
            "shadowtest BTCUSDT",
        ),
        (
            "trade [MARKET...]",
            "start LIVE trading after review + typed confirmation",
            "trade ETHTMN",
        ),
        (
            "stop [MARKET...]",
            "stop everything, or only these markets (their orders are cancelled)",
            "stop ETHTMN",
        ),
        ("flatten [MARKET...]", "sell every non-TMN balance (incl. USDT) to TMN", "flatten"),
        (
            "change FROM TO",
            "switch between backtest, shadowtest and trade (6 transitions)",
            "change shadowtest trade",
        ),
        ("resume", "lift a hard drawdown stop (human decision)", "resume"),
    ],
    "Testing": [
        (
            "backtest [--from --to --markets --timeframe]",
            "walk-forward backtest; no options = config",
            "backtest --from 2026-10-10 --markets BTCUSDT",
        ),
        (
            "doctor",
            "connectivity, keys, trading credit, fees, clock skew, BoxAPI",
            "doctor --quick",
        ),
        ("history sync", "fetch missing candles (30 days, incremental)", "history sync"),
        ("research spread|basis", "read-only edge studies (no orders)", "research basis"),
    ],
    "Results": [
        ("status", "mode, equity, PnL, drawdown, schedule, markets, strategies, feed", "status"),
        ("watch", "live view; q / Quit closes the view, the engine keeps running", "watch"),
        (
            "rslt backtest|shadowtest",
            "summary of the latest run: return, DD, trades, win rate, fees",
            "rslt shadowtest",
        ),
        ("sum [DATE]", "daily market + agent summary (facts + LLM explanation)", "sum yesterday"),
        ("log [N]", "all logs (paged), or entry N in full", "log 42"),
        ("export", "zip of decisions, orders, equity, LLM calls, reports, config", "export"),
        (
            "details [COMMAND]",
            "a view with every detail (same as adding --full); default: status",
            "details reason 12",
        ),
    ],
    "Explain": [
        ("reason [N]", "static: all decisions, or decision N's numbers in full", "reason 17"),
        (
            "exp-reason [N]",
            "LLM explanation of `reason N` (anonymized, fixed format)",
            "exp-reason 17",
        ),
        (
            "agent now|replay ID",
            "ask for an agent decision now / reproduce a recorded one",
            "agent replay 9",
        ),
    ],
    "Guardrails": [
        (
            "config guardrail",
            "show guardrails with bounds; edit them (applied live)",
            "config guardrail",
        ),
        (
            "autoset guardrail",
            "LLM suggests values; you accept all, some or none",
            "autoset guardrail",
        ),
    ],
    "System": [
        (
            "markets [--all]",
            "allowed universe: volume, spread, fees, round-trip (no keys needed)",
            "markets",
        ),
        ("setup", "first-run wizard: local keys or remote viewing", "setup"),
        ("health", "the engine's /health JSON", "health"),
        (
            "engine run --mode",
            "run the engine in the foreground (hosting)",
            "engine run --mode shadow",
        ),
        ("help [COMMAND]", "this list, or one command's options", "help backtest"),
    ],
}

PANEL = {cmd.split()[0]: group for group, rows in GROUPS.items() for cmd, _d, _e in rows}


def render_help(console: Console) -> None:
    console.print(
        "[key]wallexer[/] · automated spot trading on Wallex · `wallexer` alone opens the shell\n"
        "[muted]global: --remote reads status/watch/log/reason/rslt/sum from the server (read-only)[/]"
    )
    for group, rows in GROUPS.items():
        t = table(group, [("Command", "text"), ("What it does", "text"), ("Example", "text")])
        for cmd, desc, ex in rows:
            t.add_row(f"[key]{cmd}[/]", desc, f"[muted]wallexer {ex}[/]")
        console.print(t)
    console.print("[muted]`wallexer help <command>` shows that command's options.[/]")
