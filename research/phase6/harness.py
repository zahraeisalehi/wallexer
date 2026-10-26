"""Phase 6 harness: shared data access, cost model, statistics and the decision rule.

Everything here follows docs/research/phase6-prereg.md. Units: P&L in TMN for CAPITAL notional.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RDB = ROOT / "data" / "research" / "phase6.db"
EDB = ROOT / "data" / "wallexer.db"
CAPITAL = 1_000_000.0
Z_STAR = 2.4
K_TOTAL = 56
EVENT_END = datetime(2026, 10, 26, 20, 20, tzinfo=UTC).timestamp()  # assumption, see prereg
DD_LIMIT = 0.04 * CAPITAL
SEED = 6
MARKETS = ["BTCTMN", "ETHTMN", "XAUTTMN", "USDTTMN", "BTCUSDT", "ETHUSDT", "XAUTUSDT"]
MAKER_CONFIRMED = {
    "BTCTMN"
}  # live smoke test 2026-10-22; others cannot be confirmed without orders
DAY = 86400.0

# --------------------------------------------------------------------------------- data

_cache: dict[tuple, dict[str, np.ndarray]] = {}


def bars(symbol: str, res: str, source: str = "wallex") -> dict[str, np.ndarray]:
    key = (source, symbol, res)
    if key not in _cache:
        c = sqlite3.connect(RDB)
        rows = c.execute(
            "SELECT ts, o, h, l, c, v FROM bars WHERE source=? AND symbol=? AND res=? ORDER BY ts",
            key,
        ).fetchall()
        c.close()
        a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
        _cache[key] = {k: a[:, i] for i, k in enumerate("tohlcv")}
    return _cache[key]


def resample(b: dict[str, np.ndarray], seconds: int) -> dict[str, np.ndarray]:
    """Aggregate bars into fixed UTC buckets (open of first, max/min, close of last, sum v)."""
    if len(b["t"]) == 0:
        return b
    g = (b["t"] // seconds).astype(np.int64)
    idx = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    end = np.r_[idx[1:], len(g)]
    return {
        "t": g[idx] * float(seconds),
        "o": b["o"][idx],
        "h": np.maximum.reduceat(b["h"], idx),
        "l": np.minimum.reduceat(b["l"], idx),
        "c": b["c"][end - 1],
        "v": np.add.reduceat(b["v"], idx),
    }


# --------------------------------------------------------------------------------- costs


@dataclass
class Costs:
    maker: dict[str, float]
    taker: dict[str, float]
    half_spread: dict[str, float]
    source: dict[str, str] = field(default_factory=dict)

    def taker_side(self, m: str) -> float:
        return self.taker[m] + self.half_spread[m]

    def side(self, m: str, maker_ok: bool) -> float:
        return self.maker[m] if maker_ok else self.taker_side(m)

    def usdt_cycle(self) -> float:
        """One TMN→USDT→TMN cycle (two USDTTMN taker sides)."""
        return 2 * self.taker_side("USDTTMN")


def load_costs() -> Costs:
    c = sqlite3.connect(RDB)
    row = c.execute("SELECT v FROM meta WHERE k='fees'").fetchone()
    c.close()
    live = json.loads(row[0]) if row else {}
    maker, taker, src = {}, {}, {}
    for m in MARKETS:
        doc = (0.0025, 0.0030) if m.endswith("TMN") else (0.0010, 0.0013)  # documented table
        if isinstance(live.get(m), list) and live[m][2] == "exchange":
            maker[m], taker[m], src[m] = float(live[m][0]), float(live[m][1]), "exchange (live)"
        else:
            maker[m], taker[m], src[m] = doc[0], doc[1], "documented table"
    e = sqlite3.connect(f"file:{EDB}?mode=ro", uri=True)
    hs = {}
    for m in MARKETS:
        r = e.execute(
            "SELECT best_bid, best_ask FROM depth_snapshots WHERE symbol=? AND best_bid IS NOT NULL",
            (m,),
        ).fetchall()
        vals = [
            (float(a) - float(b)) / ((float(a) + float(b)) / 2) / 2
            for b, a in r
            if b and a and float(a) > float(b)
        ]
        if vals:
            hs[m] = float(np.median(vals))
        else:
            b1 = bars(m, "1")
            hs[m] = float(np.median((b1["h"] - b1["l"]) / b1["c"])) / 4
    e.close()
    return Costs(maker, taker, hs, src)


# --------------------------------------------------------------------------------- statistics


def block_bootstrap_ci(x: np.ndarray, n_boot: int = 1000, seed: int = SEED) -> tuple[float, float]:
    """95% CI of the mean, circular block bootstrap, block = ceil(n^(1/3))."""
    n = len(x)
    if n < 2:
        return (float("nan"), float("nan"))
    b = max(1, math.ceil(n ** (1 / 3)))
    rng = np.random.default_rng(seed)
    nb = math.ceil(n / b)
    starts = rng.integers(0, n, size=(n_boot, nb))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]) % n
    means = x[idx.reshape(n_boot, -1)[:, :n]].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def max_dd(pnl: np.ndarray) -> float:
    if len(pnl) == 0:
        return 0.0
    eq = np.r_[0.0, np.cumsum(pnl)]
    return float(np.max(np.maximum.accumulate(eq) - eq))


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class Stats:
    n: int
    mu: float
    sd: float
    t: float
    ci: tuple[float, float]
    n90: float
    maxdd: float
    rate_per_day: float  # units per day (trades/day, or 1 for per-day series)
    n_remaining: float
    p_profit: float

    def row(self) -> dict:
        return self.__dict__ | {"ci": list(self.ci)}


def stats(x: np.ndarray, span_days: float, per_day: bool, now: float | None = None) -> Stats:
    x = np.asarray(x, dtype=float)
    n = len(x)
    t_rem_days = max(0.0, (EVENT_END - (now or time.time())) / DAY)
    if n < 2:
        return Stats(
            n,
            float(x.mean()) if n else 0.0,
            0.0,
            0.0,
            (math.nan, math.nan),
            math.inf,
            max_dd(x),
            0.0,
            0.0,
            0.0,
        )
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
    rate = 1.0 if per_day else n / max(span_days, 1e-9)
    n_rem = t_rem_days * rate
    n90 = (1.2816 * sd / mu) ** 2 if mu > 0 else math.inf
    p = phi(math.sqrt(n_rem) * mu / sd) if sd > 0 and n_rem > 0 else (1.0 if mu > 0 else 0.0)
    return Stats(n, mu, sd, t, block_bootstrap_ci(x), n90, max_dd(x), rate, n_rem, p)


def verdict(s: Stats, c2_sig: int | None) -> dict[str, bool | None]:
    return {
        "a": bool(s.n >= 2 and s.mu > 0 and s.ci[0] > 0),
        "b": bool(s.t >= Z_STAR),
        "c": bool(s.n90 <= s.n_remaining),
        "d": bool(s.maxdd <= DD_LIMIT),
        "e": None if c2_sig is None else bool(c2_sig <= 10),
    }


# --------------------------------------------------------------------------------- splits


def split_ts(t: np.ndarray) -> float:
    """Time at 2/3 of the span: tune before, judge after."""
    return float(t[0] + (t[-1] - t[0]) * 2 / 3)


def daily_sum(ts: np.ndarray, pnl: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Per-UTC-day P&L over [lo, hi): days with no position count as 0."""
    d0, d1 = int(lo // DAY), int(math.ceil(hi / DAY))
    out = np.zeros(max(0, d1 - d0))
    m = (ts >= lo) & (ts < hi)
    np.add.at(out, (ts[m] // DAY).astype(int) - d0, pnl[m])
    return out


# --------------------------------------------------------------------------------- trades


@dataclass
class Trades:
    """Closed trades of one config: entry/exit times, market, P&L in TMN."""

    entry: list[float] = field(default_factory=list)
    exit: list[float] = field(default_factory=list)
    market: list[str] = field(default_factory=list)
    pnl: list[float] = field(default_factory=list)

    def add(self, e: float, x: float, m: str, p: float) -> None:
        self.entry.append(e)
        self.exit.append(x)
        self.market.append(m)
        self.pnl.append(p)

    def between(self, lo: float, hi: float) -> Trades:
        out = Trades()
        for e, x, m, p in zip(self.entry, self.exit, self.market, self.pnl, strict=True):
            if lo <= e < hi:
                out.add(e, x, m, p)
        return out

    def arr(self) -> np.ndarray:
        return np.array(self.pnl, dtype=float)

    def charge_usdt_cycle(self, costs: Costs) -> Trades:
        """One TMN→USDT→TMN cycle per segment for USDT-market trades, spread over those trades."""
        usd = [i for i, m in enumerate(self.market) if m.endswith("USDT")]
        if usd:
            each = costs.usdt_cycle() * CAPITAL / len(usd)
            for i in usd:
                self.pnl[i] -= each
        return self
