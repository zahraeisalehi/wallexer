"""`wallexer markets`: the allowed universe as a Rich table."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from wallexer.cli.fmt import human, pct, yes_no
from wallexer.config import Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.universe import UniverseEntry, build_universe


def _tier(e: UniverseEntry) -> str:
    # "*" (yellow) = config-default fee: no exchange data for this market.
    return f"[yellow]{e.fee_tier}[/]" if e.fee.source == "default" else e.fee_tier


async def run(settings: Settings, console: Console, *, show_all: bool = False) -> None:
    async with WallexClient(settings) as client:
        fees = None
        fee_note = "fees: n/a (no API key / sub-account id)"
        if settings.has_private:
            try:
                fees = await client.fees()
                if fees.source == "default":
                    fee_note = f"[yellow]fees: config default {pct(fees.default.maker)} (no data from /v1/account/fee)[/]"
                else:
                    fee_note = (
                        f"fees: {len(fees.rates)} markets from /v1/account/fee ({fees.source})"
                    )
            except (ApiError, TransportError) as e:
                fee_note = f"fees: unavailable ({str(e)[:60]})"
        with console.status("Loading markets and order books…"):
            uni = await build_universe(client, settings, fees=fees)

    # --all: every allowed market, same ranking (markets without a book sort last).
    rows = sorted(uni.candidates, key=lambda e: e.score, reverse=True) if show_all else uni.entries
    title = "Allowed spot markets" if show_all else f"Universe (top {settings.cfg.universe.top_n})"
    t = Table(title=title, header_style="bold cyan", show_lines=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column("Symbol", style="bold")
    t.add_column("Quote")
    t.add_column("Vol 24h (TMN)", justify="right")
    t.add_column("Spread bps", justify="right")
    t.add_column("Fee tier M/T %", justify="right")
    t.add_column("Round-trip bps", justify="right")
    t.add_column("Score", justify="right")
    t.add_column("Zero fee", justify="center")
    t.add_column("MARKET ord", justify="center")
    for i, e in enumerate(rows, 1):
        sp, rt = e.spread_bps, e.round_trip_bps
        t.add_row(
            str(i),
            e.market.symbol,
            e.market.quote_asset,
            human(e.quote_volume_tmn),
            "–" if sp is None else f"{sp:.1f}",
            _tier(e),
            "–" if rt is None else f"{rt:.1f}",
            human(e.score) if rt is not None else "–",
            yes_no(e.market.is_zero_fee),
            # Red "no": MARKET disabled → exits use aggressive LIMIT orders instead.
            yes_no(e.market.market_orders_enabled, bad_when=False),
        )
    console.print(t)
    ex = uni.excluded
    console.print(
        f"[dim]{len(uni.candidates)} allowed spot markets; excluded: "
        + ", ".join(f"{k}={v}" for k, v in ex.items())
        + f"\ndepth: {uni.depth_source} · {fee_note} · USDT/TMN ≈ {human(uni.usdt_tmn)}"
        + "\nRound-trip = 2 × taker fee + spread (bps). Rank/score = 24h volume (TMN) / round-trip bps."
        + "\nMARKET ord = no → MARKET orders disabled on that market; exits use aggressive LIMIT.[/]"
    )
