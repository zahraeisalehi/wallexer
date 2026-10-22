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
