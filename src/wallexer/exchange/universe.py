"""Tradable universe: spot markets not listed in data/not_allowed.txt, ranked by liquidity.

not_allowed filtering happens here at load time and again in the risk gate (is_allowed),
as defense in depth.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import structlog

from wallexer.config import Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.fees import FeeTable
from wallexer.exchange.models import Book, FeeRate, Market
from wallexer.exchange.rules import MarketRules

log = structlog.get_logger(__name__)

USDT_TMN = "USDTTMN"


def load_not_allowed(path: Path) -> frozenset[str]:
    """`BASE/QUOTE` lines → Wallex symbols `BASEQUOTE`. A missing file is an error, not 'allow all'."""
    if not path.exists():
        raise FileNotFoundError(f"not_allowed list missing: {path}")
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.replace("/", "").upper())
    return frozenset(out)


def is_allowed(symbol: str, forbidden: frozenset[str]) -> bool:
    return symbol.replace("/", "").upper() not in forbidden


def market_allowed(m: Market, forbidden: frozenset[str]) -> bool:
    # Check both the exchange symbol and base+quote, in case a symbol is ever formatted oddly.
    return is_allowed(m.symbol, forbidden) and is_allowed(m.base_asset + m.quote_asset, forbidden)


@dataclass
class UniverseEntry:
    market: Market
    rules: MarketRules
    quote_volume_tmn: Decimal
    fee: FeeRate  # exchange rate, or the config default (fee.source == "default")
    book: Book | None = None

    @property
    def spread_bps(self) -> Decimal | None:
        return self.book.spread_bps() if self.book else None

    @property
    def fee_tier(self) -> str:
        """e.g. '0.10/0.13' (maker/taker %); '*' suffix when it is the config default."""
        label = f"{self.fee.maker * 100:.2f}/{self.fee.taker * 100:.2f}"
        return label + "*" if self.fee.source == "default" else label

    @property
    def round_trip_bps(self) -> Decimal | None:
        """Cost of entering and exiting with taker orders: 2 x taker fee + spread."""
        sp = self.spread_bps
        return None if sp is None else 2 * self.fee.taker * Decimal(10000) + sp

    @property
    def score(self) -> Decimal:
        """Liquidity per unit of round-trip cost (TMN volume / bps). Higher = better."""
        rt = self.round_trip_bps
        return self.quote_volume_tmn / rt if rt and rt > 0 else Decimal(0)


@dataclass
class Universe:
    entries: list[UniverseEntry]  # ranked by score (volume / round-trip cost), after filters
    candidates: list[UniverseEntry]  # all allowed spot markets, by volume
    depth_source: str  # "depth/all" | "v1/depth fallback (<reason>)"
    usdt_tmn: Decimal | None
    excluded: dict[str, int] = field(default_factory=dict)
    unavailable: frozenset[str] = frozenset()  # trading-credit package restrictions

    @property
    def symbols(self) -> list[str]:
        return [e.market.symbol for e in self.entries]

    @property
    def limit_only(self) -> frozenset[str]:
        """Universe markets without MARKET orders (opt-in; risk gate halves their max position)."""
        return frozenset(
            e.market.symbol for e in self.entries if not e.market.market_orders_enabled
        )

    @property
    def rules(self) -> dict[str, MarketRules]:
        return {e.market.symbol: e.rules for e in self.candidates}

    @property
    def usdt_conversion_bps(self) -> Decimal | None:
        """Cost of one TMN→USDT→TMN inventory cycle via USDTTMN, in bps of the converted amount.

        Buying USDT and selling it back is exactly a USDTTMN round trip (2 x taker + spread).
        It is paid once per inventory cycle (an allocator decision), not per USDT-market trade.
        """
        for e in self.candidates:
            if e.market.symbol == USDT_TMN:
                return e.round_trip_bps
        return None

    def round_trip_incl_conversion_bps(self, e: UniverseEntry) -> Decimal | None:
        """USDT markets only: per-trade round trip + one full conversion cycle.

        Worst case (a single trade per cycle). With several trades per cycle the conversion is
        shared, so the real per-trade cost lies between round_trip_bps and this value.
        """
        if e.market.quote_asset != "USDT":
            return None
        rt, conv = e.round_trip_bps, self.usdt_conversion_bps
        return None if rt is None or conv is None else rt + conv


def usdt_tmn_price(markets: list[Market], books: dict[str, Book] | None = None) -> Decimal | None:
    if books and (b := books.get(USDT_TMN)) and b.best_bid and b.best_ask:
        return (b.best_bid + b.best_ask) / 2
    for m in markets:
        if m.symbol == USDT_TMN and m.price:
            return m.price
    return None


def _volume_tmn(m: Market, usdt_tmn: Decimal | None) -> Decimal:
    qv = m.quote_volume_24h or Decimal(0)
    if m.quote_asset == "TMN":
        return qv
    if m.quote_asset == "USDT" and usdt_tmn:
        return qv * usdt_tmn
    return Decimal(0)


async def fetch_books(
    client: WallexClient, symbols: list[str], concurrency: int = 4
) -> tuple[dict[str, Book], str]:
    """Prefer /depth/all (one call). Fall back to /v1/depth per symbol if it fails."""
    try:
        books = await client.depth_all()
        if books:
            return books, "depth/all"
        reason = "empty result"
    except (TransportError, ApiError) as e:
        reason = str(e)[:60]
    log.warning("universe.depth_all_failed", reason=reason)
    sem = asyncio.Semaphore(concurrency)
    books: dict[str, Book] = {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                books[sym] = await client.depth(sym)
            except (TransportError, ApiError) as e:
                log.warning("universe.depth_failed", symbol=sym, error=str(e)[:80])

    await asyncio.gather(*(one(s) for s in symbols))
    return books, f"v1/depth fallback ({reason})"


async def build_universe(
    client: WallexClient,
    settings: Settings,
    *,
    fees: FeeTable | None = None,
    unavailable: frozenset[str] = frozenset(),
    only: list[str] | None = None,
) -> Universe:
    """`unavailable`: the package's unavailableMarkets. `only`: restrict to these symbols."""
    ucfg = settings.cfg.universe
    forbidden = load_not_allowed(settings.cfg.path(ucfg.not_allowed_path))
    markets = await client.markets()

    excluded = {"not_spot": 0, "not_allowed": 0, "unavailable": 0, "quote": 0, "limit_only": 0}
    kept: list[Market] = []
    for m in markets:
        if not m.is_spot:
            excluded["not_spot"] += 1
        elif not market_allowed(m, forbidden):
            excluded["not_allowed"] += 1
        elif not market_allowed(m, unavailable):
            excluded["unavailable"] += 1
        elif m.quote_asset not in ucfg.quote_assets:
            excluded["quote"] += 1
        elif not m.market_orders_enabled and not ucfg.include_limit_only and m.symbol != USDT_TMN:
            # Default universe: MARKET-enabled markets only (exits can always be immediate).
            excluded["limit_only"] += 1
        elif only and m.symbol not in only:
            excluded.setdefault("not_selected", 0)
            excluded["not_selected"] += 1
        else:
            kept.append(m)

    usdt = usdt_tmn_price(markets)
    fcfg = settings.cfg.fees
    default_fee = (fees.default if fees else None) or FeeRate(
        makerFeeRate=fcfg.default_maker, takerFeeRate=fcfg.default_taker, source="default"
    )
    candidates = sorted(
        (
            UniverseEntry(
                m,
                MarketRules.from_market(m),
                _volume_tmn(m, usdt),
                fee=fees.get(m.symbol) if fees else default_fee,
            )
            for m in kept
        ),
        key=lambda e: e.quote_volume_tmn,
        reverse=True,
    )

    # For the fallback, fetch books for every market above the volume floor (~80 calls, not
    # ~120): ranking is by volume / round-trip cost, so a cheap-fee market with lower volume
    # can outrank a busier one and must not be cut before its spread is known.
    # USDTTMN is always needed for USDT valuation.
    want = [e.market.symbol for e in candidates if e.quote_volume_tmn >= ucfg.min_quote_volume_tmn]
    if USDT_TMN not in want:
        want.append(USDT_TMN)
    books, source = await fetch_books(client, want)
    for e in candidates:
        e.book = books.get(e.market.symbol)
    usdt = usdt_tmn_price(markets, books) or usdt

    entries: list[UniverseEntry] = []
    excluded.update(low_volume=0, wide_spread=0, no_book=0)
    for e in candidates:
        if e.quote_volume_tmn < ucfg.min_quote_volume_tmn:
            excluded["low_volume"] += 1
        elif e.spread_bps is None:
            excluded["no_book"] += 1
        elif e.spread_bps > ucfg.max_spread_bps:
            excluded["wide_spread"] += 1
        else:
            entries.append(e)
    entries.sort(key=lambda e: e.score, reverse=True)
    n = min(ucfg.top_n, ucfg.max_markets)
    return Universe(entries[:n], candidates, source, usdt, excluded, unavailable)
