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
        bars_by_tf = {
            tf: {s: load_bars(conn, s, res_of[tf], since) for s in symbols}
            for tf in cfg.backtest.timeframes
        }
        console.print(
            f"Grid: mean_reversion {cfg.backtest.grid_mean_reversion} + momentum {cfg.backtest.grid_momentum} "
            f"× timeframes {cfg.backtest.timeframes} = [bold]{len(walkforward.grid_configs(settings))} configs[/]"
        )
        with Progress(console=console) as prog:
            task = prog.add_task("walk-forward", total=len(walkforward.grid_configs(settings)))
            wf = walkforward.run(
                settings,
                bars_by_tf,
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
