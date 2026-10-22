"""History sync and walk-forward backtest commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from wallexer.config import Settings
from wallexer.data.history import sync_history
from wallexer.exchange.client import WallexClient
from wallexer.exchange.prop import load_unavailable
from wallexer.exchange.universe import load_not_allowed, market_allowed
from wallexer.store.db import init_db


async def universe_symbols(client: WallexClient, settings: Settings) -> list[str]:
    """The tradable set without order books: spot, allowed, available, MARKET-enabled by default."""
    cfg = settings.cfg
    forbidden = load_not_allowed(cfg.path(cfg.universe.not_allowed_path))
    unavailable = load_unavailable(cfg.path(cfg.universe.unavailable_path))
    out = []
    for m in await client.markets():
        if not m.is_spot or m.quote_asset not in cfg.universe.quote_assets:
            continue
        if not (market_allowed(m, forbidden) and market_allowed(m, unavailable)):
            continue
        if not m.market_orders_enabled and not cfg.universe.include_limit_only:
            continue
        out.append(m.symbol)
    return sorted(out)


def _d(ts: int | None) -> str:
    return "–" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


async def run_history_sync(settings: Settings, console: Console, days: int) -> None:
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        async with WallexClient(settings) as client:
            symbols = await universe_symbols(client, settings)
            with console.status(f"Syncing {days}d of candles for {len(symbols)} markets…"):
                reports = await sync_history(client, conn, symbols, days)
    finally:
        conn.close()
    t = Table(title=f"Candle history (target {days} days)", header_style="bold cyan")
    for col in (
        "Market",
        "API calls",
        "First 1-min",
        "Last 1-min",
        "Days",
        "1-min bars",
        "60-min bars",
        "1-min gaps",
    ):
        t.add_column(col, justify="right")
    for r in reports:
        t.add_row(
            r.symbol,
            str(r.calls),
            _d(r.first_ts),
            _d(r.last_ts),
            f"{r.days:.1f}",
            f"{r.bars_1m:,}",
            f"{r.bars_60m:,}",
            f"{r.gap_pct:.2f}%",
        )
    console.print(t)
    console.print(
        "[dim]5/15-min bars are resampled from 1-min (the API has no 5/15 resolution).[/]"
    )


def median_spreads(conn, symbols: list[str]) -> tuple[dict[str, Decimal], list[str]]:
    """Median spread (bps) from the last 24 h of stored depth snapshots (>= 100 rows)."""
    out: dict[str, Decimal] = {}
    missing = []
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    for s in symbols:
        rows = conn.execute(
            "SELECT best_bid, best_ask FROM depth_snapshots WHERE symbol=? AND ts >= ?"
            " AND best_bid IS NOT NULL AND best_ask IS NOT NULL",
            (s, since),
        ).fetchall()
        sp = sorted(
            (Decimal(a) - Decimal(b)) / ((Decimal(a) + Decimal(b)) / 2) * 10000
            for b, a in rows
            if Decimal(a) > Decimal(b) > 0
        )
        if len(sp) >= 100:
            out[s] = sp[len(sp) // 2]
        else:
            missing.append(s)
    return out, missing


async def run_walk_forward(settings: Settings, console: Console) -> None:
    from wallexer.backtest import report, walkforward
    from wallexer.data.history import load_bars
    from wallexer.exchange.rules import MarketRules

    cfg = settings.cfg
    conn = init_db(cfg.path(cfg.store.db_path))
    try:
        async with WallexClient(settings) as client:
            symbols = await universe_symbols(client, settings)
            with console.status("Incremental history sync…"):
                await sync_history(client, conn, symbols, cfg.backtest.history_days)
            fees = await client.fees()
            markets = {m.symbol: m for m in await client.markets() if m.is_spot}
            spreads, missing = median_spreads(conn, symbols)
            meta: dict[str, str] = {
                s: f"{v:.2f} (median 24h snapshots)" for s, v in spreads.items()
            }
            for s in missing:
                book = await client.depth(s)
                spreads[s] = book.spread_bps() or Decimal(0)
                meta[s] = f"{spreads[s]:.2f} (live /v1/depth)"
        rules = {s: MarketRules.from_market(m) for s, m in markets.items()}
        quotes = {s: markets[s].quote_asset for s in symbols}
        since = int(datetime.now(UTC).timestamp()) - cfg.backtest.history_days * 86400
        res_of = {1: "1", 5: "5", 15: "15", 60: "60"}

        # Bars are loaded per timeframe inside the run (memory-bounded for the 1 GB server).
        def load_tf(tf: int) -> dict[str, list]:
            return {s: load_bars(conn, s, res_of[tf], since) for s in symbols}

        firsts, lasts = [], []
        for s in symbols:
            lo, hi = conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol=? AND resolution='1' AND ts >= ?",
                (s, since),
            ).fetchone()
            if lo is not None:
                firsts.append(lo)
                lasts.append(hi)
        span = (max(firsts), min(lasts))
        console.print(
            f"Grid: mean_reversion {cfg.backtest.grid_mean_reversion} + momentum {cfg.backtest.grid_momentum} "
            f"× timeframes {cfg.backtest.timeframes} = [bold]{len(walkforward.grid_configs(settings))} configs[/]"
        )
        with Progress(console=console) as prog:
            task = prog.add_task("walk-forward", total=len(walkforward.grid_configs(settings)))
            wf = walkforward.run(
                settings,
                load_tf,
                span,
                quotes,
                rules,
                fees,
                spreads,
                meta,
                progress=lambda done, total: prog.update(task, completed=done),
            )
        path = cfg.path(f"data/reports/walkforward-{datetime.now(UTC):%Y%m%dT%H%M%S}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        run_id = report.persist(conn, wf, settings, path)
        path.write_text(report.markdown(wf, settings, fees, run_id), encoding="utf-8")
        _print_rechecks(conn, wf, console)
        report.render(wf, settings, fees, console, run_id)
        console.print(f"[dim]report: {path}[/]")
    finally:
        conn.close()


async def run_basis_study(settings: Settings, console: Console) -> None:
    """Research: executable triangle basis on stored simultaneous order-book snapshots."""
    from wallexer.research.basis_study import BASES, load_cycles, pct, study

    cfg = settings.cfg
    conn = init_db(cfg.path(cfg.store.db_path))
    try:
        async with WallexClient(settings) as client:
            fees = await client.fees()
        symbols = ["USDTTMN"] + [f"{b}{q}" for b in BASES for q in ("TMN", "USDT")]
        cycles, skew = load_cycles(conn, symbols)
    finally:
        conn.close()
    if not cycles:
        console.print(
            "No complete snapshot cycles yet. Run `wallexer shadowtest` to collect books."
        )
        return
    span_h = (
        max(min(s.ts for s in c.values()) for c in cycles)
        - min(min(s.ts for s in c.values()) for c in cycles)
    ) / 3600
    res = study(cycles, fees)
    console.print(
        f"[bold]Triangle basis study[/] · {len(cycles)} cycles over {span_h:.2f} h · "
        f"max intra-cycle skew {skew:.2f}s · fees: TMN legs {fees.get('BTCTMN').taker * 100:.2f}% taker, "
        f"USDT legs {fees.get('BTCUSDT').taker * 100:.2f}% taker"
    )
    t = Table(header_style="bold cyan", title="Net edge per cycle (bps of the TMN put in)")
    for col in (
        "Base",
        "Dir",
        "Taker p5/p50/p95/max",
        "% >0",
        "Mix best p50/max",
        "Mix % >0",
        "@200k p50/max",
        "@200k % >0",
        "Runs >0 @200k",
        "Longest run",
        "Max size >0 (TMN) p50/max",
    ):
        t.add_column(col, justify="right")
    verdict_hits = 0
    for st in res:
        tk, mx = st.taker_bps, st.mix_bps
        a2 = [v for v in st.at200k_bps if v is not None]
        runs = st.runs()
        repeatable = [r for r in runs if r[0] >= 2]
        verdict_hits += len(repeatable)
        sizes = [x for x in st.max_size if x > 0]
        t.add_row(
            st.base,
            "A buy-TMN" if st.direction == "A" else "B sell-TMN",
            f"{pct(tk, 0.05):.1f} / {pct(tk, 0.5):.1f} / {pct(tk, 0.95):.1f} / {max(tk):.1f}",
            f"{sum(v > 0 for v in tk) / len(tk) * 100:.1f}%",
            f"{pct(mx, 0.5):.1f} / {max(mx):.1f}",
            f"{sum(v > 0 for v in mx) / len(mx) * 100:.1f}%",
            f"{pct(a2, 0.5):.1f} / {max(a2):.1f}" if a2 else "no depth",
            f"{sum(v > 0 for v in a2) / max(len(a2), 1) * 100:.1f}%",
            str(len(runs)),
            f"{max((r[0] for r in runs), default=0)} cyc / {max((r[1] for r in runs), default=0):.0f}s",
            f"{pct(sizes, 0.5):,.0f} / {max(sizes):,.0f}" if sizes else "–",
        )
    console.print(t)
    console.print(
        "[dim]Taker = all 3 legs taker at executable top-of-book (spreads included). Mix = best single "
        "maker leg (rests at its own best price, maker fee): an upper bound, the maker leg may never fill. "
        "@200k = walking stored top-10 levels for 200,000 TMN. Runs = consecutive cycles (~3 s) with net "
        "edge > 0 at 200k, all taker.[/]"
    )
    if verdict_hits:
        console.print(
            f"[green]Verdict: {verdict_hits} repeatable (>= 2 consecutive cycles) all-taker opportunities at 200k TMN.[/]"
        )
    else:
        console.print(
            "[red]Verdict: NO repeatable, positive, net-of-fee all-taker opportunity at >= 200,000 TMN in this sample.[/]"
        )


async def run_spread_scan(
    settings: Settings, console: Console, collect: bool, hours: float
) -> None:
    """Research: spread-capture scan (collect books/trades, or analyze what was collected)."""
    from wallexer.research import spread_scan as sc

    cfg = settings.cfg
    conn = init_db(cfg.path(cfg.store.db_path))
    conn.executescript(sc.SCHEMA)
    try:
        async with WallexClient(settings) as client:
            symbols = await sc.scan_symbols(client, settings)
            if collect:
                console.print(
                    f"Collecting books + trades for {len(symbols)} markets for {hours} h "
                    f"(≤ {sc.SCAN_CALLS_PER_WINDOW} calls / {sc.WINDOW_S:.0f} s)."
                )
                await sc.collect(client, conn, symbols, hours)
                return
            fees = await client.fees()
            quotes = {m.symbol: m.quote_asset for m in await client.markets()}
        res = sc.analyze(conn, fees, quotes)
    finally:
        conn.close()
    if not res:
        conn = init_db(cfg.path(cfg.store.db_path))
        n, t0, t1 = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM scan_books").fetchone()
        conn.close()
        if not n:
            console.print(
                "No scan data yet: run `wallexer research spread --collect --hours 4` first."
            )
        else:
            console.print(
                f"{n} book samples over {(t1 - t0) / 60:.0f} min: not enough yet. Each attempt needs "
                f"{max(sc.HORIZONS_MIN)} min of follow-up data; rerun later."
            )
        return
    span = res and max(m.books for m in res)
    t = Table(
        header_style="bold cyan",
        title=f"Spread capture scan · {len(res)} markets · up to {span} book samples each",
    )
    for col in (
        "Market",
        "Maker %",
        "Spread med/p25 bps",
        "Net edge bps",
        "Prints/h",
        "Attempts",
        "Both filled through 5/15/30m",
        "Both filled touch 15m",
        "|Drift| 15m med/p90 bps",
        "E[15m] bps",
        "Pass",
    ):
        t.add_column(col, justify="right")
    for m in res:
        t.add_row(
            m.symbol,
            f"{m.maker_fee * 100:.2f}",
            f"{m.spread_med:.1f} / {m.spread_p25:.1f}",
            f"[green]{m.net_edge_med:.1f}[/]" if m.net_edge_med > 10 else f"{m.net_edge_med:.1f}",
            f"{m.trades_per_h:.0f}",
            str(m.attempts),
            " / ".join(f"{m.fill_through[h] * 100:.0f}%" for h in sc.HORIZONS_MIN),
            f"{m.fill_touch[15] * 100:.0f}%",
            f"{m.drift_med[15]:.1f} / {m.drift_p90[15]:.1f}",
            f"{m.expectancy_15:+.1f}",
            "[green]✓[/]" if m.passes else "",
        )
    console.print(t)
    console.print(
        "[dim]Net edge = median spread − 2 × maker fee. Both filled = a resting bid at best bid AND ask at "
        "best ask were both traded through (<, >) within the horizon; touch = at or through (optimistic, "
        "ignores queue). E[15m] = mean per-attempt result in bps: both fill → spread − 2 maker; one side → "
        "maker in, taker out at the book 15 min later; none → 0. Prints may be undercounted on very busy "
        "markets (/v1/trades returns only the latest prints per poll). USDT markets also carry the USDT "
        "conversion cost.[/]"
    )
    winners = [m for m in res if m.passes]
    if winners:
        console.print(
            "[green]Candidates (net edge > 10 bps, two-sided through-fills ≥ 30% in 15 min, p90 drift < edge): "
            + ", ".join(f"{m.symbol} (E {m.expectancy_15:+.1f} bps)" for m in winners)
            + "[/]"
        )
    else:
        console.print(
            "[red]Verdict: no market shows net edge > 10 bps with frequent two-sided fills and low drift.[/]"
        )


def _print_rechecks(conn, wf, console: Console) -> None:
    """The LLM agent's 'recheck' requests (last 24 h), shown first with their IS/OOS rows."""
    import json as _json

    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    reqs = {
        (p["strategy"], int(p["timeframe"]), p["market"])
        for (p_s,) in conn.execute(
            "SELECT payload FROM events WHERE kind='agent.recheck_request' AND ts >= ?", (since,)
        )
        for p in [_json.loads(p_s)]
    }
    if not reqs:
        return
    from wallexer.alloc.promotion import stats

    t = Table(
        title="Agent recheck requests (best IS params of that strategy × timeframe, per market)",
        header_style="bold magenta",
    )
    for col in ("Strategy", "TF", "Market", "Params", "IS n / net", "OOS n / net"):
        t.add_column(col)
    for strat, tf, market in sorted(reqs):
        rs = [r for r in wf.results if r.strategy == strat and r.timeframe == tf]
        if not rs:
            t.add_row(strat, f"{tf}m", market, "not in grid", "–", "–")
            continue
        best = max(rs, key=lambda r: stats(r.is_.by_market().get(market, [])).net)
        si, so = (
            stats(best.is_.by_market().get(market, [])),
            stats(best.oos.by_market().get(market, [])),
        )
        t.add_row(
            strat,
            f"{tf}m",
            market,
            str(best.params),
            f"{si.n} / {si.net:,.0f}",
            f"{so.n} / {so.net:,.0f}",
        )
    console.print(t)
