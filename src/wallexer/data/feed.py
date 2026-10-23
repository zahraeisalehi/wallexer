"""Market feed: polls order books, trades and 1-min candles for the universe.

- Depth: /depth/all when it works (tried once a minute; it 503s as of 2026-10-22), else
  /v1/depth per market. Trades every 10 s, candles every 60 s.
- Rate budget: the market-data limit is 600 requests per 39 s window (user-confirmed). Intervals
  are stretched at startup so planned calls stay under `feed_budget_frac` of it.
- Book health: crossed/locked (bid >= ask), stale (no fresh fetch for 3 intervals, or "frozen":
  the top unchanged for at least FROZEN_MIN_POLLS consecutive polls and max(frozen_book_s,
  FROZEN_MIN_POLLS x depth interval), AND >= FROZEN_MIN_TRADES trades printed *after* the last
  change; one or two prints at the touch can legitimately leave the top unchanged). Prints are
  matched by their exchange timestamp, not by when we polled them: a trades poll returns the last
  50 prints (an hour or more on a quiet market), so counting by poll time flagged quiet books as
  frozen under slow polling. Unhealthy books -> no signals + an event (rate-limited per market and
  issue).
- Candles: 1-min bars live (seeded from the stored history), resampled to 5/15-min on change;
  60-min bars are native and polled every 5 minutes.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from decimal import Decimal

import structlog

from wallexer.config import Settings
from wallexer.data.candles import Bar, completed, fetch_bars, resample
from wallexer.data.features import book_health
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.models import Book, Trade
from wallexer.store import repo
from wallexer.store.db import log_event

log = structlog.get_logger(__name__)
WINDOW_BARS = 130  # features look back this many bars on any timeframe
MAX_BARS = WINDOW_BARS * 15 + 60  # enough 1-min bars to resample 130 15-min bars
FROZEN_MIN_TRADES = 3
FROZEN_MIN_POLLS = 5
H60_POLL_S = 300
EVENT_EVERY_S = 60.0


def _trade_ts(t: Trade) -> float:
    """Exchange time of a print (ISO, e.g. 2026-10-23T13:01:09Z); 0 if unparseable (not counted)."""
    try:
        return datetime.fromisoformat(t.timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


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
        self._trades_since_change: dict[str, int] = {}
        self._unchanged_polls: dict[str, int] = {}
        self.bars: dict[str, list[Bar]] = {s: [] for s in symbols}  # 1-min
        self.bars60: dict[str, list[Bar]] = {s: [] for s in symbols}
        self._tf_cache: dict[tuple[str, int], tuple[int, list[Bar]]] = {}
        self._h60_next = 0.0
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
                self._trades_since_change[sym] = 0
                self._unchanged_polls[sym] = 0
            else:
                self._unchanged_polls[sym] = self._unchanged_polls.get(sym, 0) + 1
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
                changed = self._book_sig.get(sym)
                after = sum(1 for t in fresh if changed and _trade_ts(t) > changed[1])
                self._trades_since_change[sym] = self._trades_since_change.get(sym, 0) + after

    def load_from_store(self) -> None:
        """Seed live bars from the stored history (run history sync first)."""
        from wallexer.data.history import load_bars

        now = int(time.time())
        for sym in self.symbols:
            self.bars[sym] = load_bars(self.conn, sym, "1", now - MAX_BARS * 60)
            self.bars60[sym] = load_bars(self.conn, sym, "60", now - WINDOW_BARS * 3600 - 3600)

    def bars_tf(self, sym: str, tf: int) -> list[Bar]:
        """Completed bars of timeframe `tf` (1/5/15 resampled from 1-min; 60 native)."""
        if tf == 60:
            return self.bars60.get(sym, [])
        ones = self.bars.get(sym, [])
        if tf == 1:
            return ones
        key = (sym, tf)
        last = ones[-1].t if ones else 0
        cached = self._tf_cache.get(key)
        if cached is None or cached[0] != last:
            cached = (last, completed(resample(ones, tf), tf))
            self._tf_cache[key] = cached
        return cached[1]

    async def poll_candles(self) -> None:
        now = int(time.time())
        if now >= self._h60_next:
            self._h60_next = now + H60_POLL_S
            for sym in self.symbols:
                fresh60 = completed(
                    await fetch_bars(self.client, sym, now - 2 * 86400, now, "60"), 60
                )
                if fresh60:
                    have = {b.t: b for b in self.bars60[sym]}
                    have.update({b.t: b for b in fresh60})
                    self.bars60[sym] = [have[t] for t in sorted(have)][-(WINDOW_BARS + 10) :]
                    repo.upsert_candles(self.conn, sym, "60", fresh60)
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
            prints = self._trades_since_change.get(sym, 0)
            window = max(ecfg.frozen_book_s, FROZEN_MIN_POLLS * self.depth_interval)
            if (
                sig
                and now - sig[1] > window
                and self._unchanged_polls.get(sym, 0) >= FROZEN_MIN_POLLS
                and prints >= FROZEN_MIN_TRADES
            ):
                problem = "stale"  # book frozen while the market kept trading
        if problem is not None:
            key = (sym, problem)
            if now - self._events_at.get(key, 0.0) >= EVENT_EVERY_S:
                self._events_at[key] = now
                kind = "feed.book_crossed" if problem == "crossed" else f"feed.book_{problem}"
                sig = self._book_sig.get(sym)
                log_event(
                    self.conn,
                    kind,
                    {
                        "symbol": sym,
                        "age_s": None if age is None else round(age, 1),
                        # how long the top has not changed, and prints since (frozen check)
                        "unchanged_s": None if not sig else round(now - sig[1], 1),
                        "prints_since_change": self._trades_since_change.get(sym, 0),
                    },
                )
        return problem

    def spread_bps(self, sym: str) -> Decimal | None:
        b = self.books.get(sym)
        return b.spread_bps() if b else None
