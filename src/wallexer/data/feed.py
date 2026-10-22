"""Market feed: polls order books, trades and 1-min candles for the universe.

- Depth: /depth/all when it works (tried once a minute; it 503s as of 2026-10-22), else
  /v1/depth per market. Trades every 10 s, candles every 60 s.
- Rate budget: the market-data limit is 600 requests per 39 s window (user-confirmed). Intervals
  are stretched at startup so planned calls stay under `feed_budget_frac` of it.
- Book health: crossed/locked (bid >= ask), stale (no fresh fetch for 3 intervals, or the book
  unchanged for frozen_book_s while trades printed). Unhealthy books -> no signals + an event
  (rate-limited per market and issue).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from decimal import Decimal

import structlog

from wallexer.config import Settings
from wallexer.data.candles import Bar, completed, fetch_bars
from wallexer.data.features import book_health
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.models import Book, Trade
from wallexer.store import repo
from wallexer.store.db import log_event

log = structlog.get_logger(__name__)
MAX_BARS = 600
EVENT_EVERY_S = 60.0


class MarketFeed:
    def __init__(
        self, client: WallexClient, conn: sqlite3.Connection, settings: Settings, symbols: list[str]
    ) -> None:
        self.client = client
        self.conn = conn
        self.settings = settings
        self.symbols = symbols
        ecfg = settings.cfg.engine
        self.depth_interval = ecfg.depth_interval_s
        self.trades_interval = ecfg.trades_interval_s
        self.candles_interval = ecfg.candles_interval_s
        self.books: dict[str, Book] = {}
        self.book_ts: dict[str, float] = {}
        self._book_sig: dict[str, tuple[str, float]] = {}  # signature, last change ts
        self._last_trade_ts: dict[str, float] = {}
        self.bars: dict[str, list[Bar]] = {s: [] for s in symbols}
        self._seen: dict[str, set[tuple]] = {s: set() for s in symbols}
        self._new_trades: dict[str, list[Trade]] = {s: [] for s in symbols}
        self._events_at: dict[tuple[str, str], float] = {}
        self.depth_source = "v1/depth"
        self._depth_all_next = 0.0
        self.errors = 0
        self.plan_intervals()

    # ------------------------------------------------------------------ budget

    def plan_intervals(self) -> dict[str, float]:
        rl = self.settings.cfg.ratelimit.market
        n, w = len(self.symbols), rl.window_s
        budget = rl.limit * self.settings.cfg.engine.feed_budget_frac
        other = n * (w / self.trades_interval) + n * (w / self.candles_interval) + 2 * w / 30 + 4
        depth_calls = n * (w / self.depth_interval)
        if depth_calls + other > budget and budget > other:
            self.depth_interval = n * w / (budget - other)
            log.warning("feed.depth_interval_stretched", interval_s=round(self.depth_interval, 2))
        planned = n * (w / self.depth_interval) + other
        return {
            "planned_calls_per_window": round(planned),
            "budget": round(budget),
            "depth_interval_s": self.depth_interval,
        }

    # ------------------------------------------------------------------ polling

    async def poll_depth(self) -> None:
        now = time.time()
        got: dict[str, Book] = {}
        if now >= self._depth_all_next:
            self._depth_all_next = now + 60
            try:
                allb = await self.client.depth_all(retries=0)  # probe; fallback below
                got = {s: allb[s] for s in self.symbols if s in allb}
                self.depth_source = "depth/all"
            except (ApiError, TransportError):
                self.depth_source = "v1/depth"
        if not got:
            sem = asyncio.Semaphore(4)

            async def one(sym: str) -> None:
                async with sem:
                    try:
                        got[sym] = await self.client.depth(sym)
                    except (ApiError, TransportError) as e:
                        self.errors += 1
                        log.warning("feed.depth_failed", symbol=sym, error=str(e)[:80])

            await asyncio.gather(*(one(s) for s in self.symbols))
        ts = time.time()
        for sym, book in got.items():
            self.books[sym] = book
            self.book_ts[sym] = ts
            sig = f"{book.best_bid}|{book.best_ask}|{book.bid[:1]}|{book.ask[:1]}"
            prev = self._book_sig.get(sym)
            if prev is None or prev[0] != sig:
                self._book_sig[sym] = (sig, ts)
            bids = sorted(book.bid, key=lambda lv: lv.price, reverse=True)[:10]
            asks = sorted(book.ask, key=lambda lv: lv.price)[:10]
            repo.insert_depth_snapshot(
                self.conn,
                symbol=sym,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                bids=[[str(lv.price), str(lv.quantity)] for lv in bids],
                asks=[[str(lv.price), str(lv.quantity)] for lv in asks],
            )

    async def poll_trades(self) -> None:
        for sym in self.symbols:
            try:
                trades = await self.client.trades(sym)
            except (ApiError, TransportError):
                self.errors += 1
                continue
            seen = self._seen[sym]
            fresh = []
            for t in trades:
                key = (t.timestamp, t.price, t.quantity, t.is_buy_order)
                if key not in seen:
                    seen.add(key)
                    fresh.append(t)
            if len(seen) > 2000:  # bounded memory; old keys never come back
                self._seen[sym] = {
                    (t.timestamp, t.price, t.quantity, t.is_buy_order) for t in trades
                }
            if fresh:
                self._new_trades[sym].extend(fresh)
                self._last_trade_ts[sym] = time.time()

    async def load_history(self, days: int) -> None:
        now = int(time.time())
        for sym in self.symbols:
            bars = await fetch_bars(self.client, sym, now - days * 86400, now)
            self.bars[sym] = completed(bars)
            repo.upsert_candles(self.conn, sym, "1", self.bars[sym])

    async def poll_candles(self) -> None:
        now = int(time.time())
        for sym in self.symbols:
            fresh = completed(await fetch_bars(self.client, sym, now - 15 * 60, now))
            if not fresh:
                continue
            have = {b.t: b for b in self.bars[sym]}
            for b in fresh:
                have[b.t] = b
            self.bars[sym] = [have[t] for t in sorted(have)][-MAX_BARS:]
            repo.upsert_candles(self.conn, sym, "1", fresh)

    # ------------------------------------------------------------------ consumers

    def consume_trades(self) -> dict[str, list[Trade]]:
        out = {s: v for s, v in self._new_trades.items() if v}
        self._new_trades = {s: [] for s in self.symbols}
        return out

    def issue(self, sym: str, now: float) -> str | None:
        ecfg = self.settings.cfg.engine
        ts = self.book_ts.get(sym)
        age = None if ts is None else now - ts
        problem = book_health(
            self.books.get(sym),
            age_s=age,
            stale_after_s=ecfg.stale_after_intervals * self.depth_interval,
        )
        if problem is None:
            sig = self._book_sig.get(sym)
            traded = self._last_trade_ts.get(sym, 0.0)
            if sig and now - sig[1] > ecfg.frozen_book_s and traded > sig[1]:
                problem = "stale"  # book frozen while the market traded
        if problem is not None:
            key = (sym, problem)
            if now - self._events_at.get(key, 0.0) >= EVENT_EVERY_S:
                self._events_at[key] = now
                kind = "feed.book_crossed" if problem == "crossed" else f"feed.book_{problem}"
                log_event(
                    self.conn,
                    kind,
                    {"symbol": sym, "age_s": None if age is None else round(age, 1)},
                )
        return problem

    def spread_bps(self, sym: str) -> Decimal | None:
        b = self.books.get(sym)
        return b.spread_bps() if b else None
