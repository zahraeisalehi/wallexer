"""The 10 pre-registered candidates (docs/research/phase6-prereg.md).

Each `seg_*` evaluates one config on the time segment [lo, hi): positions start flat at `lo` and
indicators may use data before `lo` (the past). It returns a Result: the per-unit P&L series (TMN,
CAPITAL notional), the segment length, whether units are days, and what the C2 control needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from harness import CAPITAL, DAY, MAKER_CONFIRMED, Costs, Trades, bars, daily_sum, resample


@dataclass
class Result:
    values: np.ndarray
    span_days: float
    per_day: bool
    c2: dict = field(default_factory=dict)  # control inputs
    info: dict = field(default_factory=dict)  # measured values to report


def _maker_ok(m: str, maker_all: bool) -> bool:
    return maker_all or m in MAKER_CONFIRMED


def _tmn_daily(sym: str) -> dict[str, np.ndarray]:
    """Daily bars of an asset priced in TMN (USDT markets converted at USDTTMN close)."""
    b = bars(sym, "D")
    r60 = resample(bars(sym, "60"), 86400)
    if len(r60["t"]) > len(b["t"]):  # e.g. XAUTUSDT: 1 y of daily bars, 2 y of hourly
        b = r60
    if sym.endswith("TMN"):
        return b
    u = bars("USDTTMN", "D")
    common, ia, ib = np.intersect1d(b["t"], u["t"], return_indices=True)
    return {k: (b[k][ia] * (u["c"][ib] if k in "ohlc" else 1)) for k in "ohlcv"} | {"t": common}


# ============================================================================ 1 carry_usdt


def seg_carry(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    h = cfg["h"]
    b = bars("USDTTMN", "D")
    m = (b["t"] >= lo) & (b["t"] < hi)
    c = b["c"][m]
    cost = 2 * costs.taker_side("USDTTMN")
    vals = [CAPITAL * (c[i + h] / c[i] - 1 - cost) for i in range(0, len(c) - h, h)]
    return Result(
        np.array(vals),
        (hi - lo) / DAY,
        False,
        {"kind": "trade", "holds": [h * DAY] * len(vals), "markets": ["USDTTMN"]},
    )


def carry_profile(costs: Costs) -> dict[int, float]:
    """P(net profit) of rolling windows of h days over the full daily history."""
    c = bars("USDTTMN", "D")["c"]
    cost = 2 * costs.taker_side("USDTTMN")
    return {h: float(np.mean(c[h:] / c[:-h] - 1 - cost > 0)) for h in (1, 2, 7, 30)}


# ============================================================================ 2 / 10 tsmom


def _tsmom_series(sym: str, res: str, look: int, lo: float, hi: float, costs: Costs) -> tuple:
    """Bar P&L (fraction) of a long/flat, vol-scaled time-series momentum on one asset."""
    if res == "240":
        b = resample(bars(sym, "60"), 4 * 3600)
        per_year = 6 * 365
    elif res == "60":
        b = bars(sym, "60")
        per_year = 24 * 365
    else:
        b = bars(sym, "D")
        per_year = 365
    t, c = b["t"], b["c"]
    r = np.r_[0.0, c[1:] / c[:-1] - 1]
    side = costs.taker_side(sym)
    out_t, out_p, in_pos = [], [], []
    w_prev = 0.0
    for i in range(max(look, 31), len(c)):
        if t[i] < lo or t[i] >= hi:
            if t[i] >= hi:
                break
            continue
        # position decided at the close of bar i-1 (no look-ahead), earns bar i's return
        sig = c[i - 1] / c[i - 1 - look] - 1 if i - 1 - look >= 0 else 0
        vol = np.std(r[i - 31 : i - 1], ddof=1) * math.sqrt(per_year)
        w = min(1.0, 0.20 / vol) if sig > 0 and vol > 0 else 0.0
        p = w_prev * r[i] - abs(w - w_prev) * side
        out_t.append(t[i])
        out_p.append(p)
        in_pos.append(w > 0)
        w_prev = w
    return np.array(out_t), np.array(out_p), np.array(in_pos)


def seg_tsmom_usdttmn(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    res, look = cfg["res"], cfg["look"]
    t, p, pos = _tsmom_series("USDTTMN", res, look, lo, hi, costs)
    daily = daily_sum(t, p * CAPITAL, lo, hi)
    return Result(
        daily,
        (hi - lo) / DAY,
        True,
        {"kind": "day", "assets": ["USDTTMN"], "in_frac": float(pos.mean()) if len(pos) else 0},
    )


def seg_tsmom_crypto(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    res, look = cfg["res"], cfg["look"]
    total = None
    in_frac = []
    for sym in ("BTCUSDT", "ETHUSDT", "XAUTUSDT"):
        t, p, pos = _tsmom_series(sym, res, look, lo, hi, costs)
        d = daily_sum(t, p * CAPITAL / 3, lo, hi)
        total = d if total is None else total[: len(d)] + d[: len(total)]
        in_frac.append(float(pos.mean()) if len(pos) else 0)
    # one TMN↔USDT cycle per segment for the USDT inventory, spread over the days
    if total is not None and len(total) and max(in_frac) > 0:
        total = total - costs.usdt_cycle() * CAPITAL / len(total)
    return Result(
        total if total is not None else np.zeros(0),
        (hi - lo) / DAY,
        True,
        {
            "kind": "day",
            "assets": ["BTCUSDT", "ETHUSDT", "XAUTUSDT"],
            "in_frac": float(np.mean(in_frac)),
        },
    )


# ============================================================================ 3 rebal_bands


def seg_rebal(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    w_t = np.array(cfg["w"])  # USDT, XAUT, BTC
    band = cfg["band"]
    u = bars("USDTTMN", "D")
    x, b = _tmn_daily("XAUTUSDT"), _tmn_daily("BTCUSDT")
    t = np.intersect1d(np.intersect1d(u["t"], x["t"]), b["t"])
    t = t[(t >= lo) & (t < hi)]
    if len(t) < 3:
        return Result(np.zeros(0), (hi - lo) / DAY, True, {"kind": "day"})
    pu = u["c"][np.searchsorted(u["t"], t)]
    px = x["c"][np.searchsorted(x["t"], t)]
    pb = b["c"][np.searchsorted(b["t"], t)]
    prices = np.c_[pu, px, pb]
    side_usdt = costs.taker_side("USDTTMN")
    side_x, side_b = costs.taker_side("XAUTUSDT"), costs.taker_side("BTCUSDT")

    def run(rebalance: bool) -> np.ndarray:
        # start: TMN→USDT for everything, then USDT→XAUT/BTC for their weights
        start_cost = CAPITAL * (side_usdt + w_t[1] * side_x + w_t[2] * side_b)
        units = CAPITAL * w_t / prices[0]
        val = [CAPITAL - start_cost]
        for i in range(1, len(t)):
            v = units * prices[i]
            tot = v.sum()
            cost = 0.0
            if rebalance:
                w = v / tot
                if np.any(np.abs(w - w_t) / w_t > band):
                    traded = np.abs(w - w_t) * tot
                    cost = traded[1] * side_x + traded[2] * side_b
                    units = (tot - cost) * w_t / prices[i]
            val.append(tot - cost)
        val[-1] -= val[-1] * side_usdt  # back to TMN at the end
        return np.diff(np.array(val))

    rb, bh = run(True), run(False)
    return Result(
        rb,
        (hi - lo) / DAY,
        True,
        {"kind": "day", "assets": ["mix"], "in_frac": 1.0},
        {
            "excess_mu_per_day": float(np.mean(rb - bh)) if len(rb) else 0.0,
            "bh_mu_per_day": float(np.mean(bh)) if len(bh) else 0.0,
        },
    )


# ============================================================================ 4 mr_usdt_maker


def _vwap(b: dict, w: int) -> np.ndarray:
    tp = (b["h"] + b["l"] + b["c"]) / 3
    v = np.where(b["v"] > 0, b["v"], 1e-12)
    cs_pv, cs_v = np.cumsum(tp * v), np.cumsum(v)
    out = np.empty_like(tp)
    out[:w] = np.nan
    out[w:] = (cs_pv[w:] - cs_pv[:-w]) / (cs_v[w:] - cs_v[:-w])
    return out


def _erfi(x: float) -> float:
    s = np.linspace(0, x, 2001)
    return float(2 / math.sqrt(math.pi) * np.trapezoid(np.exp(s * s), s))


def bertram_band(theta: float, sigma: float, cost: float) -> float:
    """Symmetric OU entry/exit band a* (entry −a, exit +a) maximizing (2a − c) / E[T]."""
    best, best_a = -math.inf, None
    s_eq = sigma / math.sqrt(2 * theta)
    for k in np.linspace(0.1, 3.0, 59):
        a = k * s_eq
        et = math.pi / theta * 2 * _erfi(a * math.sqrt(theta) / sigma)
        mu = (2 * a - cost) / et
        if mu > best:
            best, best_a = mu, a
    return best_a if best > 0 else math.inf


def fit_ar1(x: np.ndarray) -> tuple[float, float, float]:
    """φ, half-life (bars), σ_eq of x_t = φ x_{t-1} + ε."""
    x = x[np.isfinite(x)]
    a, b = x[:-1], x[1:]
    ph = float(np.dot(a - a.mean(), b - b.mean()) / np.dot(a - a.mean(), a - a.mean()))
    eps = b - ph * a
    if not 0 < ph < 1:
        return ph, math.inf, float(np.std(x))
    return ph, math.log(2) / -math.log(ph), float(np.std(eps) / math.sqrt(1 - ph * ph))


def seg_mr_usdt(
    cfg: dict,
    lo: float,
    hi: float,
    costs: Costs,
    maker_all: bool,
    fit_window: tuple[float, float] | None = None,
) -> Result:
    m, w = cfg["market"], cfg["anchor"]
    b = bars(m, "1")
    x = np.log(b["c"] / _vwap(b, w))
    # AR(1) fitted on the tuning window (in-sample), applied unchanged to the segment
    flo, fhi = fit_window or (lo, hi)
    fm = (b["t"] >= flo) & (b["t"] < fhi)
    ph, hl, s_eq = fit_ar1(x[fm])
    maker = _maker_ok(m, maker_all)
    side = costs.side(m, maker)
    cost = 2 * side
    info = {
        "phi": ph,
        "half_life_min": hl,
        "sigma_eq": s_eq,
        "c_over_sigma": cost / s_eq,
        "maker": maker,
    }
    tr = Trades()
    if not (cost / s_eq <= 3 and hl <= 120):
        info["gate"] = "closed (c/σ_eq > 3 or H > 120 min)"
        return Result(
            np.zeros(0),
            (hi - lo) / 86400,
            False,
            {"kind": "trade", "holds": [], "markets": [m], "side": side},
            info,
        )
    theta = -math.log(ph)
    sigma = s_eq * math.sqrt(2 * theta)
    a = bertram_band(theta, sigma, cost)
    info["band_a"] = a
    if not math.isfinite(a):
        info["gate"] = "no positive Bertram band"
        return Result(
            np.zeros(0),
            (hi - lo) / 86400,
            False,
            {"kind": "trade", "holds": [], "markets": [m], "side": side},
            info,
        )
    vw = _vwap(b, w)
    timeout = 4 * hl * 60
    i_lo, i_hi = np.searchsorted(b["t"], lo), np.searchsorted(b["t"], hi)
    pos, ent_p, ent_t = False, 0.0, 0.0
    for i in range(max(i_lo, w + 1), i_hi):
        if not math.isfinite(vw[i - 1]):
            continue
        if not pos:
            bid = vw[i - 1] * math.exp(-a)
            if maker and b["l"][i] < bid:  # printed through our bid
                pos, ent_p, ent_t = True, bid, b["t"][i]
            elif not maker and x[i] <= -a:
                pos, ent_p, ent_t = True, b["c"][i], b["t"][i]
        else:
            ask = vw[i - 1] * math.exp(a)
            if maker and b["h"][i] > ask:
                tr.add(ent_t, b["t"][i], m, CAPITAL * (ask / ent_p - 1 - 2 * side))
                pos = False
            elif not maker and x[i] >= a:
                tr.add(ent_t, b["t"][i], m, CAPITAL * (b["c"][i] / ent_p - 1 - 2 * side))
                pos = False
            elif b["t"][i] - ent_t > timeout:  # exit falls back to taker
                tr.add(
                    ent_t,
                    b["t"][i],
                    m,
                    CAPITAL * (b["c"][i] / ent_p - 1 - side - costs.taker_side(m)),
                )
                pos = False
    tr.charge_usdt_cycle(costs)
    return Result(
        tr.arr(),
        (hi - lo) / 86400,
        False,
        {
            "kind": "trade",
            "holds": [x2 - e for e, x2 in zip(tr.entry, tr.exit, strict=True)],
            "markets": [m],
            "side": side,
        },
        info,
    )


# ============================================================================ 5 mr_bigmove_tmn

TMN_MR = ["BTCTMN", "ETHTMN", "XAUTTMN", "USDTTMN"]


def seg_bigmove(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    k, w = cfg["k"], cfg["window"]
    tr = Trades()
    for m in TMN_MR:
        b = bars(m, "1")
        c = b["c"]
        mv = np.r_[np.full(w, np.nan), c[w:] - c[:-w]]
        absmv = np.abs(mv)
        # ATR proxy: mean |W-minute change| over the last 14·W minutes
        cs = np.nancumsum(absmv)
        atr = np.full_like(c, np.nan)
        n = 14 * w
        atr[n + w :] = (cs[n + w :] - cs[w:-n]) / n
        vw = _vwap(b, w)
        side = costs.taker_side(m)
        i_lo, i_hi = np.searchsorted(b["t"], lo), np.searchsorted(b["t"], hi)
        pos, ent_p, ent_t = False, 0.0, 0.0
        for i in range(max(i_lo, n + w + 4), i_hi):
            if not pos:
                if not (math.isfinite(atr[i]) and mv[i] < -k * atr[i]):
                    continue
                if (
                    c[i] < c[i - 1] < c[i - 2] < c[i - 3]
                ):  # informed-flow filter: run of 3 down bars
                    continue
                pos, ent_p, ent_t = True, c[i], b["t"][i]
            else:
                if c[i] > vw[i] or c[i] <= ent_p * 0.98 or b["t"][i] - ent_t > DAY:
                    tr.add(ent_t, b["t"][i], m, CAPITAL * (c[i] / ent_p - 1 - 2 * side))
                    pos = False
    return Result(
        tr.arr(),
        (hi - lo) / DAY,
        False,
        {
            "kind": "trade",
            "holds": [x - e for e, x in zip(tr.entry, tr.exit, strict=True)],
            "markets": TMN_MR,
            "side": None,
        },
    )


# ============================================================================ 6 cap_momentum


def seg_capmom(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    X = cfg["X"] / 100
    b = bars("USDTTMN", "D")
    c, t = b["c"], b["t"]
    r = np.r_[np.nan, c[1:] / c[:-1] - 1]
    cost = 2 * costs.taker_side("USDTTMN")
    vals, raw = [], []
    for i in range(1, len(c) - 1):
        if lo <= t[i] < hi and r[i] > X:
            raw.append(r[i + 1])
            vals.append(CAPITAL * (r[i + 1] - cost))
    return Result(
        np.array(vals),
        (hi - lo) / DAY,
        False,
        {"kind": "trade", "holds": [DAY] * len(vals), "markets": ["USDTTMN"]},
        {
            "mean_next_day_bps": float(np.mean(raw) * 1e4) if raw else None,
            "cost_bps": cost * 1e4,
            "events": len(raw),
        },
    )


# ============================================================================ 7 nobitex_lead

NOB = {"BTC": ("BTCIRT", "BTCTMN"), "USDT": ("USDTIRT", "USDTTMN")}


def _aligned(asset: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ns, ws = NOB[asset]
    nb, wb = bars(ns, "1", "nobitex"), bars(ws, "1")
    t, ia, ib = np.intersect1d(nb["t"], wb["t"], return_indices=True)
    return t, nb["c"][ia], wb["c"][ib], wb["c"][ib]


def xcorr(asset: str, lo: float, hi: float) -> dict[int, float]:
    t, nc, wc, _ = _aligned(asset)
    m = (t >= lo) & (t < hi)
    t, nc, wc = t[m], nc[m], wc[m]
    contiguous = np.r_[False, np.diff(t) == 60]
    rn = np.where(contiguous, np.r_[np.nan, np.diff(np.log(nc))], np.nan)
    rw = np.where(contiguous, np.r_[np.nan, np.diff(np.log(wc))], np.nan)
    out = {}
    for lag in range(0, 11):
        a, b = rn[: len(rn) - lag], rw[lag:]
        ok = np.isfinite(a) & np.isfinite(b)
        out[lag] = float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() > 100 else math.nan
    return out


def seg_nobitex(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    asset, mult, lag = cfg["asset"], cfg["m"], cfg["lag"]
    t, nc, wc, _ = _aligned(asset)
    ws = NOB[asset][1]
    side = costs.taker_side(ws)
    thr = mult * 2 * side
    tr = Trades()
    i_lo, i_hi = np.searchsorted(t, lo), np.searchsorted(t, hi)
    i = max(i_lo, lag + 1)
    while i < i_hi - lag:
        if t[i] - t[i - lag] == lag * 60 and t[i + lag] - t[i] == lag * 60:
            move = nc[i] / nc[i - lag] - 1
            if move > thr:
                tr.add(t[i], t[i + lag], ws, CAPITAL * (wc[i + lag] / wc[i] - 1 - 2 * side))
                i += lag
                continue
        i += 1
    return Result(
        tr.arr(),
        (hi - lo) / DAY,
        False,
        {"kind": "trade", "holds": [lag * 60.0] * len(tr.pnl), "markets": [ws], "side": side},
    )


# ============================================================================ 8 grid_wide


def first_passage(m: str, g: float, lo: float, hi: float) -> float:
    """P(+g before −g) from hourly starts; a bar touching both counts as down (conservative)."""
    b = bars(m, "1")
    i_lo, i_hi = np.searchsorted(b["t"], lo), np.searchsorted(b["t"], hi)
    ups = downs = 0
    for s in range(i_lo, i_hi, 60):
        p = b["c"][s]
        hi_hit = np.flatnonzero(b["h"][s + 1 : i_hi] >= p * (1 + g))
        lo_hit = np.flatnonzero(b["l"][s + 1 : i_hi] <= p * (1 - g))
        u = hi_hit[0] if len(hi_hit) else math.inf
        d = lo_hit[0] if len(lo_hit) else math.inf
        if u == d == math.inf:
            continue
        if u < d:
            ups += 1
        else:
            downs += 1
    return ups / (ups + downs) if ups + downs else math.nan


def seg_grid(cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool) -> Result:
    m, g = cfg["market"], cfg["g"] / 100
    b = bars(m, "1")
    maker = _maker_ok(m, maker_all)
    side = costs.side(m, maker)
    lots_n, lot = 5, CAPITAL / 5
    i_lo, i_hi = np.searchsorted(b["t"], lo), np.searchsorted(b["t"], hi)
    if i_hi - i_lo < 10:
        return Result(
            np.zeros(0), (hi - lo) / DAY, False, {"kind": "trade", "holds": [], "markets": [m]}
        )
    anchor = b["c"][i_lo]
    open_lots: dict[int, tuple[float, float]] = {}  # level → (buy price, time)
    tr = Trades()
    for i in range(i_lo + 1, i_hi):
        lo_p, hi_p, c = b["l"][i], b["h"][i], b["c"][i]
        for lv in range(1, lots_n + 1):
            level = anchor * (1 - g) ** lv
            if lv in open_lots:
                bp, bt = open_lots[lv]
                target = bp * (1 + g)
                if (maker and hi_p > target) or (not maker and hi_p >= target):
                    sell = target if maker else c
                    tr.add(bt, b["t"][i], m, lot * (sell / bp - 1 - 2 * side))
                    del open_lots[lv]
            elif (maker and lo_p < level) or (not maker and lo_p <= level):
                open_lots[lv] = (level if maker else c, b["t"][i])
        if not open_lots and c > anchor * (1 + g):
            anchor = c  # re-center upward when flat
    for bp, bt in open_lots.values():  # mark open lots to market at the segment end
        tr.add(
            bt, b["t"][i_hi - 1], m, lot * (b["c"][i_hi - 1] / bp - 1 - side - costs.taker_side(m))
        )
    tr.charge_usdt_cycle(costs)
    return Result(
        tr.arr(),
        (hi - lo) / DAY,
        False,
        {
            "kind": "trade",
            "holds": [x - e for e, x in zip(tr.entry, tr.exit, strict=True)],
            "markets": [m],
            "side": side,
            "notional": lot,
        },
        {"maker": maker},
    )


# ============================================================================ 9 maker_markout


def markout_data(m: str) -> tuple[np.ndarray, ...]:
    import sqlite3
    from datetime import datetime

    from harness import EDB

    e = sqlite3.connect(f"file:{EDB}?mode=ro", uri=True)
    snaps = e.execute(
        "SELECT ts, best_bid, best_ask FROM depth_snapshots WHERE symbol=? AND best_bid IS NOT NULL"
        " ORDER BY ts",
        (m,),
    ).fetchall()
    trades = e.execute(
        "SELECT ts, price, is_buy FROM scan_trades WHERE symbol=? ORDER BY ts", (m,)
    ).fetchall()
    e.close()
    st = np.array([datetime.fromisoformat(s[0]).timestamp() for s in snaps])
    bid = np.array([float(s[1]) for s in snaps])
    ask = np.array([float(s[2]) for s in snaps])
    tt = np.array([t[0] for t in trades], dtype=float)
    tp = np.array([float(t[1]) for t in trades])
    return st, bid, ask, tt, tp


def seg_markout(
    cfg: dict, lo: float, hi: float, costs: Costs, maker_all: bool, horizon: int = 300
) -> Result:
    m = cfg["market"]
    maker = _maker_ok(m, maker_all)
    st, bid, ask, tt, tp = markout_data(m)
    info = {"maker": maker, "snapshots": len(st), "trades": len(tt)}
    if not maker:
        info["gate"] = "limit orders unconfirmed on this market (primary: maker not allowed)"
        return Result(
            np.zeros(0),
            (hi - lo) / DAY,
            False,
            {"kind": "trade", "holds": [], "markets": [m]},
            info,
        )
    mid = (bid + ask) / 2
    fee = costs.maker[m]
    vals, used, mk = [], set(), {60: [], 300: [], 1800: []}
    for ts, p in zip(tt, tp, strict=True):
        if not lo <= ts < hi:
            continue
        j = np.searchsorted(st, ts) - 1
        if j < 0 or ts - st[j] > 30:
            continue
        key = (j, p < bid[j])
        if key in used:
            continue
        if p < bid[j]:  # a trade printed through our resting bid: we bought at the bid
            fill, sgn = bid[j], 1
        elif p > ask[j]:
            fill, sgn = ask[j], -1
        else:
            continue
        used.add(key)
        for hz in mk:
            k2 = np.searchsorted(st, ts + hz)
            if k2 < len(st) and st[k2] - (ts + hz) < 60:
                mk[hz].append(sgn * (mid[k2] - fill) / fill - fee)
        k2 = np.searchsorted(st, ts + horizon)
        if k2 < len(st) and st[k2] - (ts + horizon) < 60:
            vals.append(CAPITAL * (sgn * (mid[k2] - fill) / fill - fee))
    info["edge_bps_per_side"] = {
        hz: (float(np.mean(v) * 1e4) if v else None) for hz, v in mk.items()
    }
    info["half_spread_bps"] = costs.half_spread[m] * 1e4
    return Result(
        np.array(vals),
        (hi - lo) / DAY,
        False,
        {"kind": "trade", "holds": [float(horizon)] * len(vals), "markets": [m], "side": fee},
        info,
    )
