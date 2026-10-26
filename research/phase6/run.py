"""Phase 6 runner: every pre-registered candidate × config, IS vs OOS, C2 control, decision rule.

Run: uv run python research/phase6/run.py   → data/research/phase6_results.json (+ console summary)
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import candidates as C  # noqa: E402
from harness import (  # noqa: E402
    CAPITAL,
    DAY,
    EVENT_END,
    K_TOTAL,
    RDB,
    SEED,
    Z_STAR,
    Costs,
    bars,
    load_costs,
    split_ts,
    stats,
    verdict,
)

NOW = time.time()

GRIDS: dict[str, list[dict]] = {
    "1 carry_usdt": [{"h": h} for h in (1, 2, 7, 30)],
    "2 tsmom_usdttmn": [
        {"res": r, "look": lk}
        for r, ls in (("D", (1, 3, 7, 14)), ("60", (24, 72, 168, 336)))
        for lk in ls
    ],
    "3 rebal_bands": [],  # filled after costs are known (b* depends on f)
    "4 mr_usdt_maker": [
        {"market": m, "anchor": a} for m in ("BTCUSDT", "ETHUSDT", "XAUTUSDT") for a in (60, 240)
    ],
    "5 mr_bigmove_tmn": [{"k": k, "window": w} for k in (3, 4, 5) for w in (60, 240)],
    "6 cap_momentum": [{"X": x} for x in (2, 3, 4)],
    "7 nobitex_lead": [{"asset": a, "m": m} for a in ("BTC", "USDT") for m in (1, 2)],
    "8 grid_wide": [{"market": m, "g": g} for m in ("XAUTUSDT", "BTCUSDT") for g in (1, 2, 3)],
    "9 maker_markout": [{"market": m} for m in C.TMN_MR + ["BTCUSDT", "ETHUSDT", "XAUTUSDT"]],
    "10 tsmom_crypto": [
        {"res": r, "look": lk}
        for r, lk in (("60", 24), ("60", 72), ("240", 6), ("240", 42), ("D", 7), ("D", 30))
    ],
}
FN = {
    "1 carry_usdt": C.seg_carry,
    "2 tsmom_usdttmn": C.seg_tsmom_usdttmn,
    "3 rebal_bands": C.seg_rebal,
    "4 mr_usdt_maker": C.seg_mr_usdt,
    "5 mr_bigmove_tmn": C.seg_bigmove,
    "6 cap_momentum": C.seg_capmom,
    "7 nobitex_lead": C.seg_nobitex,
    "8 grid_wide": C.seg_grid,
    "9 maker_markout": C.seg_markout,
    "10 tsmom_crypto": C.seg_tsmom_crypto,
}
HOLD = {
    "1 carry_usdt": False,
    "2 tsmom_usdttmn": True,
    "3 rebal_bands": True,
    "6 cap_momentum": False,
    "10 tsmom_crypto": True,
}
WALK_FORWARD = [
    "1 carry_usdt",
    "2 tsmom_usdttmn",
    "3 rebal_bands",
    "6 cap_momentum",
    "10 tsmom_crypto",
]
MAKER_SENS = ["4 mr_usdt_maker", "8 grid_wide", "9 maker_markout"]


def span(name: str) -> tuple[float, float]:
    """The candidate's data range (common to all its configs)."""
    if name in ("1 carry_usdt", "6 cap_momentum"):
        t = bars("USDTTMN", "D")["t"]
    elif name in ("2 tsmom_usdttmn", "10 tsmom_crypto"):
        t = np.intersect1d(bars("USDTTMN", "60")["t"], bars("ETHUSDT", "60")["t"])
    elif name == "3 rebal_bands":
        t = C._tmn_daily("XAUTUSDT")["t"]
    elif name == "9 maker_markout":
        st, *_, tt, _tp = C.markout_data("BTCTMN")
        return float(max(st.min(), tt.min())), float(min(st.max(), tt.max()))
    else:
        t = bars("BTCUSDT", "1")["t"]
    return float(t[0]), float(t[-1]) + 1


def run_cfg(name: str, cfg: dict, lo: float, mid: float, hi: float, costs: Costs, maker_all: bool):
    fn = FN[name]
    if name == "4 mr_usdt_maker":  # AR(1) fitted in-sample only, also for the OOS run
        ins = fn(cfg, lo, mid, costs, maker_all, fit_window=(lo, mid))
        oos = fn(cfg, mid, hi, costs, maker_all, fit_window=(lo, mid))
    else:
        ins, oos = fn(cfg, lo, mid, costs, maker_all), fn(cfg, mid, hi, costs, maker_all)
    return ins, oos


# ------------------------------------------------------------------------------- control C2


def c2_control(name: str, res: C.Result, lo: float, hi: float, costs: Costs) -> dict:
    """100 seeds of random entries matched to the candidate's OOS trades; count significant seeds."""
    c2 = res.c2
    sig, ts = 0, []
    if c2.get("kind") == "trade":
        n = len(res.values)
        holds = c2.get("holds") or []
        if n < 2 or not holds:
            return {"seeds": 0, "significant": None, "note": "fewer than 2 OOS trades: no control"}
        notional = c2.get("notional", CAPITAL)
        for seed in range(100):
            rng = np.random.default_rng(SEED * 1000 + seed)
            vals = []
            for _ in range(n):
                m = c2["markets"][rng.integers(len(c2["markets"]))]
                h = holds[rng.integers(len(holds))]
                b = (
                    bars(m, "D")
                    if h >= DAY and name in ("1 carry_usdt", "6 cap_momentum")
                    else bars(m, "1")
                )
                ok = np.flatnonzero((b["t"] >= lo) & (b["t"] < hi - h))
                if len(ok) == 0:
                    continue
                i = ok[rng.integers(len(ok))]
                j = min(np.searchsorted(b["t"], b["t"][i] + h), len(b["t"]) - 1)
                side = c2.get("side") or costs.taker_side(m)
                vals.append(notional * (b["c"][j] / b["c"][i] - 1 - 2 * side))
            s = stats(np.array(vals), (hi - lo) / DAY, False, NOW)
            ts.append(s.t)
            sig += int(s.t >= Z_STAR and s.mu > 0)
    elif c2.get("kind") == "day":
        days = len(res.values)
        if days < 2:
            return {"seeds": 0, "significant": None, "note": "no OOS days"}
        rets = []
        for a in c2["assets"]:
            if a == "mix":  # equal-weight USDT/XAUT/BTC in TMN
                parts = [C._tmn_daily(x) for x in ("XAUTUSDT", "BTCUSDT")] + [bars("USDTTMN", "D")]
                t = parts[0]["t"]
                for p_ in parts[1:]:
                    t = np.intersect1d(t, p_["t"])
                r = np.mean(
                    [
                        np.diff(p_["c"][np.searchsorted(p_["t"], t)])
                        / p_["c"][np.searchsorted(p_["t"], t)][:-1]
                        for p_ in parts
                    ],
                    axis=0,
                )
                t = t[1:]
            else:
                b = C._tmn_daily(a) if not a.endswith("TMN") else bars(a, "D")
                t, r = b["t"][1:], np.diff(b["c"]) / b["c"][:-1]
            m = (t >= lo) & (t < hi)
            rets.append((r[m], costs.taker_side(a) if a != "mix" else costs.taker_side("XAUTUSDT")))
        frac = c2.get("in_frac", 0.5) or 0.5
        for seed in range(100):
            rng = np.random.default_rng(SEED * 1000 + seed)
            tot = np.zeros(min(len(r) for r, _ in rets))
            for r, side in rets:
                r = r[: len(tot)]
                pos = rng.random(len(r)) < frac
                flips = np.abs(np.diff(np.r_[0, pos.astype(int)]))
                tot += CAPITAL / len(rets) * (pos * r - flips * side)
            s = stats(tot, (hi - lo) / DAY, True, NOW)
            ts.append(s.t)
            sig += int(s.t >= Z_STAR and s.mu > 0)
    else:
        return {"seeds": 0, "significant": None}
    return {
        "seeds": 100,
        "significant": sig,
        "median_t": float(np.median(ts)) if ts else None,
        "p95_t": float(np.percentile(ts, 95)) if ts else None,
    }


# ------------------------------------------------------------------------------- walk-forward


def walk_forward(name: str, grid: list[dict], lo: float, hi: float, costs: Costs) -> dict:
    """Rolling origin: before each of the last 12 months, re-tune on all earlier data; test the month."""
    months = 12
    edges = [hi - (months - k) * 30 * DAY for k in range(months + 1)]
    pooled, picks = [], []
    for a, b in zip(edges[:-1], edges[1:], strict=True):
        if a - lo < 60 * DAY:
            continue
        best, best_t = None, -math.inf
        for cfg in grid:
            r = FN[name](cfg, lo, a, costs, False)
            s = stats(r.values, (a - lo) / DAY, r.per_day, NOW)
            if s.n >= 2 and s.t > best_t:
                best, best_t = cfg, s.t
        if best is None:
            continue
        r = FN[name](best, a, b, costs, False)
        pooled.extend(r.values.tolist())
        picks.append(best)
    s = stats(np.array(pooled), (edges[-1] - edges[0]) / DAY, HOLD.get(name, False), NOW)
    return {"months": len(picks), "n": s.n, "mu": s.mu, "t": s.t, "ci": list(s.ci), "picks": picks}


# ------------------------------------------------------------------------------- properties


def properties(costs: Costs, mr_info: dict) -> dict:
    out = {}
    for m in C.TMN_MR + ["BTCUSDT", "ETHUSDT", "XAUTUSDT"]:
        b = bars(m, "1")
        ok = np.r_[False, np.diff(b["t"]) == 60]
        r = np.diff(np.log(b["c"]))[ok[1:]]
        r = r[np.isfinite(r)]
        a = np.sort(np.abs(r[r != 0]))[::-1]
        kk = max(10, int(0.025 * len(a)))
        hill = 1 / np.mean(np.log(a[:kk] / a[kk]))
        vr = {}
        for q in (5, 15, 60):
            rq = np.convolve(r, np.ones(q), "valid")
            vr[q] = float(np.var(rq) / (q * np.var(r)))
        # Hurst by rescaled range over window sizes 16..4096
        ns, rs = [], []
        for w in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
            vals = []
            for s0 in range(0, len(r) - w, w):
                x = r[s0 : s0 + w]
                y = np.cumsum(x - x.mean())
                sd = x.std()
                if sd > 0:
                    vals.append((y.max() - y.min()) / sd)
            if vals:
                ns.append(w)
                rs.append(np.mean(vals))
        hurst = float(np.polyfit(np.log(ns), np.log(rs), 1)[0])
        out[m] = {
            "hill_alpha": float(hill),
            "VR": vr,
            "hurst": hurst,
            "half_spread_bps": costs.half_spread[m] * 1e4,
        }
    u = bars("USDTTMN", "D")
    r = np.diff(np.log(u["c"]))
    out["USDTTMN_daily"] = {
        "sigma_full_pct": float(np.std(r) * 100),
        "drift_full_bps_per_day": float(np.mean(r) * 1e4),
        "sigma_30d_pct": float(np.std(r[-30:]) * 100),
        "drift_30d_bps_per_day": float(np.mean(r[-30:]) * 1e4),
        "days": len(r),
        "from": time.strftime("%Y-%m-%d", time.gmtime(u["t"][0])),
    }
    out["half_lives"] = mr_info
    return out


# ------------------------------------------------------------------------------- main


def main() -> None:
    costs = load_costs()
    f = (costs.taker_side("XAUTUSDT") + costs.taker_side("BTCUSDT")) / 2
    bstar = (12 * f) ** (1 / 3)
    GRIDS["3 rebal_bands"] = [
        {"w": w, "band": b}
        for w in ((0.5, 0.3, 0.2), (1 / 3, 1 / 3, 1 / 3))
        for b in (bstar, 0.5 * bstar, 2 * bstar)
    ]
    assert sum(len(g) for g in GRIDS.values()) == K_TOTAL
    nob_ok = len(bars("BTCIRT", "1", "nobitex")["t"]) > 0
    out = {
        "now": NOW,
        "event_end": EVENT_END,
        "t_rem_h": (EVENT_END - NOW) / 3600,
        "K": K_TOTAL,
        "z_star": Z_STAR,
        "b_star": bstar,
        "f": f,
        "costs": {
            "maker": costs.maker,
            "taker": costs.taker,
            "half_spread": costs.half_spread,
            "source": costs.source,
            "usdt_cycle": costs.usdt_cycle(),
        },
        "candidates": {},
    }
    mr_info = {}
    for name, grid in GRIDS.items():
        print("==", name, flush=True)
        if name == "7 nobitex_lead" and not nob_ok:
            out["candidates"][name] = {
                "status": "not tested (api.nobitex.ir unreachable: ConnectError)"
            }
            continue
        lo, hi = span(name)
        mid = split_ts(np.array([lo, hi]))
        rows = []
        for cfg in grid:
            ins, oos = run_cfg(name, cfg, lo, mid, hi, costs, False)
            si = stats(ins.values, (mid - lo) / DAY, ins.per_day, NOW)
            so = stats(oos.values, (hi - mid) / DAY, oos.per_day, NOW)
            rows.append(
                {
                    "cfg": cfg,
                    "is": si.row(),
                    "oos": so.row(),
                    "info_is": ins.info,
                    "info_oos": oos.info,
                    "_oos": oos,
                }
            )
            if name == "4 mr_usdt_maker":
                mr_info[f"{cfg['market']} VWAP{cfg['anchor']}"] = ins.info
            print(
                "  ",
                cfg,
                f"IS n={si.n} t={si.t:.2f} | OOS n={so.n} t={so.t:.2f} mu={so.mu:.0f}",
                flush=True,
            )
        best = max(rows, key=lambda r: (r["is"]["n"] >= 2, r["is"]["t"], r["is"]["mu"]))
        c2 = c2_control(name, best["_oos"], mid, hi, costs)
        so = stats(best["_oos"].values, (hi - mid) / DAY, best["_oos"].per_day, NOW)
        v = verdict(so, c2.get("significant"))
        extra_ok = True
        if name == "3 rebal_bands":
            extra_ok = best["info_oos"].get("excess_mu_per_day", 0) > 0
        passed = all(v[k] for k in "abcd") and v["e"] is not False and extra_ok
        entry = {
            "span": [lo, mid, hi],
            "best": best["cfg"],
            "is": best["is"],
            "oos": best["oos"],
            "rules": v,
            "extra_ok": extra_ok,
            "pass": passed,
            "c2": c2,
            "info": best["info_oos"] | {"is": best["info_is"]},
            "grid": [{k: r[k] for k in ("cfg", "is", "oos")} for r in rows],
        }
        if name in MAKER_SENS:  # maker allowed on all markets (reported only)
            sens = []
            for cfg in grid:
                ins, oos = run_cfg(name, cfg, lo, mid, hi, costs, True)
                sens.append(
                    {
                        "cfg": cfg,
                        "is": stats(ins.values, (mid - lo) / DAY, False, NOW).row(),
                        "oos": stats(oos.values, (hi - mid) / DAY, False, NOW).row(),
                        "info": oos.info,
                    }
                )
            entry["maker_sensitivity"] = sens
        if name in WALK_FORWARD:
            entry["walk_forward"] = walk_forward(name, grid, lo, hi, costs)
        if name == "1 carry_usdt":
            entry["p_profit_by_hold"] = C.carry_profile(costs)
        if name == "8 grid_wide":
            fp = {}
            for cfg in grid:
                g = cfg["g"] / 100
                rt = 2 * costs.taker_side(cfg["market"])
                fp[f"{cfg['market']} g={cfg['g']}%"] = {
                    "p_up_first_is": C.first_passage(cfg["market"], g, lo, mid),
                    "p_up_first_oos": C.first_passage(cfg["market"], g, mid, hi),
                    "threshold": 0.5 + rt / g,
                }
            entry["first_passage"] = fp
        out["candidates"][name] = entry
        print("   best", best["cfg"], "rules", v, "pass", passed, "c2", c2, flush=True)
    out["properties"] = properties(costs, mr_info)
    out["any_c2_significant"] = [
        n
        for n, e in out["candidates"].items()
        if isinstance(e, dict)
        and (e.get("c2") or {}).get("significant") not in (None,)
        and e["c2"]["significant"] > 10
    ]
    path = RDB.parent / "phase6_results.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print("wrote", path)


if __name__ == "__main__":
    main()
