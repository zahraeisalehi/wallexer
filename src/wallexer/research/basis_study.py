"""Triangle-basis study on stored order-book snapshots (research only; no engine code path).

For X in {BTC, ETH, XAUT} and both directions, per snapshot cycle (the 7 books are fetched within
~1 s of each other every ~3 s by the shadow feed):
- A "buy X on TMN": TMN -> X (buy X/TMN at asks) -> USDT (sell X/USDT at bids) -> TMN (sell USDT/TMN at bids)
- B "sell X on TMN": TMN -> USDT (buy USDT/TMN at asks) -> X (buy X/USDT at asks) -> TMN (sell X/TMN at bids)
Executable prices already contain the spreads. Reported per cycle:
- net edge at top of book, all three legs taker (real fee table);
- best maker/taker mix: one leg resting at its own best price with the maker fee, the other two
  taker (an upper bound: the maker leg may never fill);
- edge at 200,000 TMN walking the stored top-10 levels, and the largest TMN size with net edge > 0.
Opportunities = runs of consecutive cycles with net edge > 0 at 200k TMN (all taker).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from wallexer.exchange.fees import FeeTable

BASES = ("BTC", "ETH", "XAUT")
BPS = Decimal(10000)
ONE = Decimal(1)
CYCLE_GAP_S = 1.5  # snapshots closer than this belong to the same polling cycle

Levels = list[tuple[Decimal, Decimal]]  # (price, qty), best first


@dataclass
class Snap:
    ts: float
    bids: Levels
    asks: Levels


@dataclass
class Leg:
    symbol: str
    side: str  # BUY | SELL (of the base asset of `symbol`)
    taker: Decimal
    maker: Decimal


def legs_for(base: str, direction: str, fees: FeeTable) -> list[Leg]:
    def leg(sym: str, side: str) -> Leg:
        f = fees.get(sym)
        return Leg(sym, side, f.taker, f.maker)

    if direction == "A":
        return [leg(f"{base}TMN", "BUY"), leg(f"{base}USDT", "SELL"), leg("USDTTMN", "SELL")]
    return [leg("USDTTMN", "BUY"), leg(f"{base}USDT", "BUY"), leg(f"{base}TMN", "SELL")]


def load_cycles(
    conn: sqlite3.Connection, symbols: Sequence[str]
) -> tuple[list[dict[str, Snap]], float]:
    """Group snapshots into polling cycles; keep cycles where every symbol is present.

    Returns (cycles, max intra-cycle timestamp skew in seconds).
    """
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT ts, symbol, bids, asks FROM depth_snapshots WHERE symbol IN ({placeholders}) ORDER BY ts",
        tuple(symbols),
    ).fetchall()
    cycles: list[dict[str, Snap]] = []
    cur: dict[str, Snap] = {}
    start = last = None
    skew = 0.0
    for ts_s, sym, bids, asks in rows:
        ts = datetime.fromisoformat(ts_s).timestamp()
        if last is not None and ts - last > CYCLE_GAP_S:
            if len(cur) == len(symbols):
                cycles.append(cur)
                skew = max(skew, last - start)
            cur, start = {}, None
        start = ts if start is None else start
        last = ts
        cur[sym] = Snap(
            ts,
            [(Decimal(p), Decimal(q)) for p, q in json.loads(bids)],
            [(Decimal(p), Decimal(q)) for p, q in json.loads(asks)],
        )
    if len(cur) == len(symbols):
        cycles.append(cur)
    return cycles, skew


def _walk(levels: Levels, side: str, amount_in: Decimal) -> Decimal | None:
    """Convert `amount_in` through one book. BUY spends quote for base (asks); SELL sells base for
    quote (bids). Returns the amount received before fees, or None if depth runs out."""
    left, out = amount_in, Decimal(0)
    for price, qty in levels:
        if side == "BUY":
            cost = price * qty
            take = min(left, cost)
            out += take / price
        else:
            take = min(left, qty)
            out += take * price
        left -= take
        if left <= 0:
            return out
    return None


def cycle_multiplier(
    cyc: dict[str, Snap], legs: Sequence[Leg], start_tmn: Decimal
) -> Decimal | None:
    """TMN out / TMN in for a fully taker cycle walking real depth; None if depth is insufficient."""
    amount = start_tmn
    for leg in legs:
        snap = cyc[leg.symbol]
        got = _walk(snap.asks if leg.side == "BUY" else snap.bids, leg.side, amount)
        if got is None:
            return None
        amount = got * (ONE - leg.taker)
    return amount / start_tmn


def top_multiplier(
    cyc: dict[str, Snap], legs: Sequence[Leg], maker_leg: int | None = None
) -> Decimal:
    """Top-of-book multiplier. maker_leg = index of the leg resting at its own best price."""
    m = ONE
    for i, leg in enumerate(legs):
        s = cyc[leg.symbol]
        maker = i == maker_leg
        if leg.side == "BUY":
            px = s.bids[0][0] if maker else s.asks[0][0]
            m = m / px
        else:
            px = s.asks[0][0] if maker else s.bids[0][0]
            m = m * px
        m *= ONE - (leg.maker if maker else leg.taker)
    return m


def max_size_tmn(
    cyc: dict[str, Snap], legs: Sequence[Leg], cap: Decimal = Decimal(500_000_000)
) -> Decimal:
    """Largest start size (TMN) whose walked cycle still has net multiplier > 1 (bisection)."""
    lo, hi = Decimal(0), cap
    m = cycle_multiplier(cyc, legs, Decimal(1000))
    if m is None or m <= ONE:
        return Decimal(0)
    for _ in range(40):
        mid = (lo + hi) / 2
        mm = cycle_multiplier(cyc, legs, mid)
        if mm is not None and mm > ONE:
            lo = mid
        else:
            hi = mid
    return lo


def pct(xs: Sequence[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


@dataclass
class DirectionStats:
    base: str
    direction: str
    cycles: int
    taker_bps: list[float] = field(default_factory=list)
    mix_bps: list[float] = field(default_factory=list)
    at200k_bps: list[float | None] = field(default_factory=list)
    max_size: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)

    def runs(self) -> list[tuple[int, float]]:
        """(length in cycles, duration s) of consecutive cycles with net edge > 0 at 200k."""
        out, n, t0 = [], 0, 0.0
        for v, ts in zip(self.at200k_bps, self.timestamps, strict=True):
            if v is not None and v > 0:
                if n == 0:
                    t0 = ts
                n += 1
                last = ts
            elif n:
                out.append((n, last - t0))
                n = 0
        if n:
            out.append((n, last - t0))
        return out


def study(
    cycles: list[dict[str, Snap]], fees: FeeTable, size_tmn: Decimal = Decimal(200_000)
) -> list[DirectionStats]:
    out = []
    for base in BASES:
        for d in ("A", "B"):
            legs = legs_for(base, d, fees)
            st = DirectionStats(base, d, len(cycles))
            for cyc in cycles:
                st.timestamps.append(min(s.ts for s in cyc.values()))
                st.taker_bps.append(float((top_multiplier(cyc, legs) - ONE) * BPS))
                st.mix_bps.append(
                    float(max((top_multiplier(cyc, legs, i) - ONE) * BPS for i in range(3)))
                )
                m = cycle_multiplier(cyc, legs, size_tmn)
                st.at200k_bps.append(None if m is None else float((m - ONE) * BPS))
                st.max_size.append(float(max_size_tmn(cyc, legs)) if st.taker_bps[-1] > 0 else 0.0)
            out.append(st)
    return out
