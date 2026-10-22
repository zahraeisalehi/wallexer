"""Incremental candle history: native 1-min and 60-min, plus 5/15-min resampled from 1-min.

Chunks (1 day for 1-min, 7 days for 60-min) are aligned to fixed boundaries and recorded in
`history_chunks`; a complete past chunk is never fetched again, so restarts only fetch what is
missing (normally just the current chunk). After each sync the 5/15-min series are rebuilt
from the stored 1-min bars for the affected days.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import structlog

from wallexer.data.candles import CHUNK_S, Bar, completed, fetch_bars, resample
from wallexer.exchange.client import WallexClient
from wallexer.store import repo
from wallexer.store.db import now_iso

log = structlog.get_logger(__name__)
RESAMPLED = (5, 15)


@dataclass
class SyncReport:
    symbol: str
    calls: int
    first_ts: int | None
    last_ts: int | None
    bars_1m: int
    bars_60m: int
    gap_pct: float  # missing 1-min bars over the covered span

    @property
    def days(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return (self.last_ts - self.first_ts) / 86400


def _chunks(resolution: str, from_ts: int, to_ts: int) -> list[tuple[int, int]]:
    size = CHUNK_S[resolution]
    start = from_ts - from_ts % size
    out = []
    while start < to_ts:
        out.append((start, start + size))
        start += size
    return out


def _done(conn: sqlite3.Connection, symbol: str, resolution: str) -> set[int]:
    rows = conn.execute(
        "SELECT chunk_start FROM history_chunks WHERE symbol=? AND resolution=? AND complete=1",
        (symbol, resolution),
    ).fetchall()
    return {r[0] for r in rows}


async def sync_symbol(
    client: WallexClient, conn: sqlite3.Connection, symbol: str, days: int, now: float | None = None
) -> SyncReport:
    now_i = int(time.time() if now is None else now)
    calls = 0
    touched_days: set[int] = set()
    for res in ("1", "60"):
        done = _done(conn, symbol, res)
        for start, end in _chunks(res, now_i - days * 86400, now_i):
            if start in done:
                continue
            bars = await fetch_bars(client, symbol, start, min(end, now_i), res)
            calls += 1
            complete = end <= now_i
            bars = completed(bars, int(res), now_i)
            with conn:
                conn.execute("BEGIN")
                repo.insert_candles(conn, symbol, res, bars)
                conn.execute(
                    "INSERT OR REPLACE INTO history_chunks (symbol, resolution, chunk_start,"
                    " chunk_end, bars, complete, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    (symbol, res, start, end, len(bars), int(complete), now_iso()),
                )
            if res == "1":
                touched_days.add(start)
    if touched_days:
        rebuild_resampled(conn, symbol, min(touched_days), max(touched_days) + 86400, now_i)
    first, last, n1 = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE symbol=? AND resolution='1'",
        (symbol,),
    ).fetchone()
    n60 = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol=? AND resolution='60'", (symbol,)
    ).fetchone()[0]
    span_min = ((last - first) // 60 + 1) if first is not None else 0
    gap = (1 - n1 / span_min) * 100 if span_min else 0.0
    return SyncReport(symbol, calls, first, last, n1, n60, round(gap, 2))


def rebuild_resampled(
    conn: sqlite3.Connection, symbol: str, from_ts: int, to_ts: int, now: int
) -> None:
    ones = load_bars(conn, symbol, "1", from_ts, to_ts)
    with conn:
        conn.execute("BEGIN")
        for m in RESAMPLED:
            bars = completed(resample(ones, m), m, now)
            repo.insert_candles(conn, symbol, str(m), bars)


async def sync_history(
    client: WallexClient, conn: sqlite3.Connection, symbols: Sequence[str], days: int
) -> list[SyncReport]:
    out = []
    for s in symbols:
        r = await sync_symbol(client, conn, s, days)
        log.info(
            "history.synced", symbol=s, calls=r.calls, days=round(r.days, 1), gap_pct=r.gap_pct
        )
        out.append(r)
    return out


def load_bars(
    conn: sqlite3.Connection, symbol: str, resolution: str, from_ts: int = 0, to_ts: int = 2**62
) -> list[Bar]:
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? AND resolution=?"
        " AND ts >= ? AND ts < ? ORDER BY ts",
        (symbol, resolution, from_ts, to_ts),
    ).fetchall()
    return [
        Bar(r[0], Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5]))
        for r in rows
    ]
