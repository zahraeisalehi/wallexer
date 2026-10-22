"""Spread-capture scan (research only; read-only API calls, no orders, no engine code path).

Question: on which allowed markets could a maker resting a bid at best bid and an ask at best ask
capture the spread net of 2 maker fees, with both sides filling often and little adverse drift?

Collector: round-robin /v1/depth + /v1/trades over every allowed, available spot market (LIMIT-only
included: the idea is maker-only), paced to <= SCAN_CALLS_PER_WINDOW per 39 s window so the
7-market shadow feed keeps priority; backs off further when x-ratelimit-remaining is low.
Analysis: see `analyze`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog

from wallexer.config import Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.fees import FeeTable
from wallexer.exchange.prop import load_unavailable
from wallexer.exchange.universe import load_not_allowed, market_allowed
from wallexer.store.db import now_iso

log = structlog.get_logger(__name__)

SCAN_CALLS_PER_WINDOW = 150  # 25% of 600 per 39 s; the shadow feed plans ~22%
WINDOW_S = 39.0
LOW_REMAINING_FRAC = 0.4  # below this, pause a window: the engine feed has priority
HORIZONS_MIN = (5, 15, 30)
SAMPLE_EVERY_S = 300  # attempt start times, per market

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_books (
    ts REAL NOT NULL, symbol TEXT NOT NULL, bid TEXT, ask TEXT, bid_qty TEXT, ask_qty TEXT
);
CREATE INDEX IF NOT EXISTS ix_scan_books ON scan_books(symbol, ts);
CREATE TABLE IF NOT EXISTS scan_trades (
    symbol TEXT NOT NULL, ts REAL NOT NULL, price TEXT NOT NULL, qty TEXT NOT NULL,
    is_buy INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts, price, qty, is_buy)
);
"""


async def scan_symbols(client: WallexClient, settings: Settings) -> list[str]:
    cfg = settings.cfg
    forbidden = load_not_allowed(cfg.path(cfg.universe.not_allowed_path))
    unavailable = load_unavailable(cfg.path(cfg.universe.unavailable_path))
    return sorted(
        m.symbol
        for m in await client.markets()
        if m.is_spot
        and m.quote_asset in cfg.universe.quote_assets
        and market_allowed(m, forbidden)
        and market_allowed(m, unavailable)
    )


def _iso_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


async def collect(
    client: WallexClient, conn: sqlite3.Connection, symbols: Sequence[str], hours: float
) -> None:
    conn.executescript(SCHEMA)
    end = time.time() + hours * 3600
    gap = WINDOW_S / SCAN_CALLS_PER_WINDOW  # seconds between calls
    passes = 0
    while time.time() < end:
        t_pass = time.time()
        for sym in symbols:
            if time.time() >= end:
                break
            try:
                book = await client.depth(sym)
                await asyncio.sleep(gap)
                trades = await client.trades(sym)
                await asyncio.sleep(gap)
            except (ApiError, TransportError) as e:
                log.warning("scan.fetch_failed", symbol=sym, error=str(e)[:80])
                await asyncio.sleep(gap * 2)
                continue
            bb = max(book.bid, key=lambda lv: lv.price, default=None)
            ba = min(book.ask, key=lambda lv: lv.price, default=None)
            with conn:
                conn.execute("BEGIN")
                conn.execute(
                    "INSERT INTO scan_books (ts, symbol, bid, ask, bid_qty, ask_qty) VALUES (?,?,?,?,?,?)",
                    (
                        time.time(),
                        sym,
                        bb and str(bb.price),
                        ba and str(ba.price),
                        bb and str(bb.quantity),
                        ba and str(ba.quantity),
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO scan_trades (symbol, ts, price, qty, is_buy) VALUES (?,?,?,?,?)",
                    [
                        (
                            sym,
                            _iso_ts(t.timestamp),
                            str(t.price),
                            str(t.quantity),
                            int(t.is_buy_order),
                        )
                        for t in trades
                    ],
                )
            lim = client.market_limiter
            if (
                lim.limit
                and lim.remaining is not None
                and lim.remaining < lim.limit * LOW_REMAINING_FRAC
            ):
                await asyncio.sleep(WINDOW_S)  # yield the window to the engine feed
        passes += 1
        log.info(
            "scan.pass",
            n=passes,
            symbols=len(symbols),
            seconds=round(time.time() - t_pass, 1),
            at=now_iso(),
        )


# ---------------------------------------------------------------------------------- analysis


@dataclass
class MarketScan:
    symbol: str
    quote: str
    maker_fee: Decimal
    books: int
    trades_per_h: float
    spread_med: float
    spread_p25: float
    net_edge_med: float  # median spread - 2 x maker fee (bps)
    attempts: int
    fill_touch: dict[int, float]  # horizon min -> share of attempts with BOTH sides filled
    fill_through: dict[int, float]
    drift_med: dict[int, float]  # |delta mid| bps
    drift_p90: dict[int, float]
    expectancy_15: float  # bps of notional per attempt (through-fills), see analyze()

    @property
    def passes(self) -> bool:
        return (
            self.net_edge_med > 10
            and self.fill_through.get(15, 0) >= 0.30
            and self.drift_p90.get(15, 1e9) < self.net_edge_med
        )


def _q(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def analyze(conn: sqlite3.Connection, fees: FeeTable, quotes: dict[str, str]) -> list[MarketScan]:
    """Per market, from the collected books and trades.

    Attempts: every SAMPLE_EVERY_S per market, a bid at best bid b0 and an ask at best ask a0.
    A side fills if a later trade prints at/through its price within the horizon:
    touch = price <= b0 / >= a0 (optimistic, ignores queue), through = < b0 / > a0 (conservative).
    Expectancy at 15 min (through fills): both -> (a0 - b0)/mid - 2 maker; one side -> that
    side's maker fill, then exit as taker at the book 15 min later; none -> 0.
    """
    out = []
    syms = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM scan_books")]
    for sym in syms:
        books = [
            (ts, float(b), float(a))
            for ts, b, a in conn.execute(
                "SELECT ts, bid, ask FROM scan_books WHERE symbol=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts",
                (sym,),
            )
            if float(a) > float(b) > 0
        ]
        trades = [
            (ts, float(p))
            for ts, p in conn.execute(
                "SELECT ts, price FROM scan_trades WHERE symbol=? ORDER BY ts", (sym,)
            )
        ]
        if len(books) < 5:
            continue
        fee = fees.get(sym)
        mk = float(fee.maker) * 1e4
        tk = float(fee.taker) * 1e4
        spreads = [(a - b) / ((a + b) / 2) * 1e4 for _, b, a in books]
        span_h = (books[-1][0] - books[0][0]) / 3600
        t_ts = [t for t, _ in trades]
        in_span = [t for t in t_ts if books[0][0] <= t <= books[-1][0]]
        b_ts = [b[0] for b in books]
        fill_touch = {h: 0 for h in HORIZONS_MIN}
        fill_through = {h: 0 for h in HORIZONS_MIN}
        drifts: dict[int, list[float]] = {h: [] for h in HORIZONS_MIN}
        exp15: list[float] = []
        attempts = 0
        next_t = books[0][0]
        for ts, b0, a0 in books:
            if ts < next_t or ts + max(HORIZONS_MIN) * 60 > books[-1][0]:
                continue
            next_t = ts + SAMPLE_EVERY_S
            attempts += 1
            mid0 = (a0 + b0) / 2
            for h in HORIZONS_MIN:
                lo, hi = bisect_right(t_ts, ts), bisect_right(t_ts, ts + h * 60)
                window = [p for _, p in trades[lo:hi]]
                if any(p <= b0 for p in window) and any(p >= a0 for p in window):
                    fill_touch[h] += 1
                bt = any(p < b0 for p in window)
                st = any(p > a0 for p in window)
                if bt and st:
                    fill_through[h] += 1
                j = min(bisect_left(b_ts, ts + h * 60), len(books) - 1)
                _, bh, ah = books[j]
                midh = (ah + bh) / 2
                drifts[h].append(abs(midh - mid0) / mid0 * 1e4)
                if h == 15:
                    if bt and st:
                        exp15.append((a0 - b0) / mid0 * 1e4 - 2 * mk)
                    elif bt:  # bought at b0 (maker), sold as taker at bid_h
                        exp15.append((bh - b0) / b0 * 1e4 - mk - tk)
                    elif st:  # sold at a0 (maker), bought back as taker at ask_h
                        exp15.append((a0 - ah) / a0 * 1e4 - mk - tk)
                    else:
                        exp15.append(0.0)
        if attempts == 0:
            continue
        sm = _q(spreads, 0.5)
        out.append(
            MarketScan(
                sym,
                quotes.get(sym, "?"),
                fee.maker,
                len(books),
                len(in_span) / span_h if span_h > 0 else 0.0,
                sm,
                _q(spreads, 0.25),
                sm - 2 * mk,
                attempts,
                {h: fill_touch[h] / attempts for h in HORIZONS_MIN},
                {h: fill_through[h] / attempts for h in HORIZONS_MIN},
                {h: _q(drifts[h], 0.5) for h in HORIZONS_MIN},
                {h: _q(drifts[h], 0.9) for h in HORIZONS_MIN},
                sum(exp15) / len(exp15) if exp15 else 0.0,
            )
        )
    return sorted(out, key=lambda m: m.expectancy_15, reverse=True)
