"""Walk-forward persistence, out-of-sample gate and report (Rich + markdown for the submission)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from wallexer.alloc.promotion import TradeStats, stats
from wallexer.backtest.walkforward import (
    STRATEGIES,
    WalkForward,
    best_per_timeframe,
    max_drawdown_pct,
)
from wallexer.config import PromotionCfg, Settings
from wallexer.exchange.fees import FeeTable
from wallexer.store.db import dumps, now_iso

ZERO = Decimal(0)


def _d(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _pf(s: TradeStats) -> str:
    pf = s.profit_factor
    return "–" if pf is None else ("inf" if pf.is_infinite() else f"{pf:.2f}")


def oos_gate(s: TradeStats, cfg: PromotionCfg) -> tuple[bool, str]:
    """The promotion gate's backtest half: OOS only, thresholds unchanged."""
    pf = s.profit_factor
    problems = []
    if s.n < cfg.min_backtest_trades:
        problems.append(f"{s.n} OOS trades < {cfg.min_backtest_trades}")
    if s.net <= 0:
        problems.append(f"OOS net {s.net:,.0f} TMN <= 0")
    if pf is None or pf <= cfg.min_profit_factor:
        problems.append(f"OOS PF {_pf(s)} <= {cfg.min_profit_factor}")
    return (not problems, "; ".join(problems) or "OOS gate passed")


def _stats_json(s: TradeStats, trades: Any = ()) -> dict[str, str | int]:
    return {
        "n": s.n,
        "net": str(s.net),
        "gross_profit": str(s.gross_profit),
        "gross_loss": str(s.gross_loss),
        "wins": s.wins,
        # for `rslt backtest` only; the gate reads the fields above
        "fees": str(sum((t.fees_tmn for t in trades), ZERO)),
    }


def stats_from_json(d: dict[str, Any]) -> TradeStats:
    return TradeStats(
        int(d["n"]),
        Decimal(d["net"]),
        Decimal(d["gross_profit"]),
        Decimal(d["gross_loss"]),
        int(d["wins"]),
    )


def selected_summary(wf: WalkForward, cfg: PromotionCfg) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in STRATEGIES:
        r = wf.selected.get(name)
        if r is None:
            out[name] = {
                "selected": None,
                "passes": False,
                "reason": "no IS-valid config (>= min IS trades and IS PF > 1)",
            }
            # Shown in `rslt backtest` so a failed strategy still has numbers; never promotable.
            cands = [x for x in wf.results if x.strategy == name]
            if cands:
                b = max(cands, key=lambda x: (x.is_stats.net, -x.is_stats.n))
                out[name]["best_is"] = {
                    "config": {"timeframe": b.timeframe, "params": b.params},
                    "is": _stats_json(b.is_stats, b.is_.trades),
                    "oos": _stats_json(b.oos_stats, b.oos.trades),
                }
            continue
        ok, why = oos_gate(r.oos_stats, cfg)
        out[name] = {
            "selected": {"timeframe": r.timeframe, "params": r.params},
            "is": _stats_json(r.is_stats, r.is_.trades),
            "oos": _stats_json(r.oos_stats, r.oos.trades),
            "passes": ok,
            "reason": why,
        }
    out["basis_tmn_usdt"] = {
        "selected": None,
        "passes": False,
        "reason": "not candle-backtested: shadow-evaluated, paper-only",
    }
    return out


def persist(
    conn: sqlite3.Connection,
    wf: WalkForward,
    settings: Settings,
    report_path: Path,
    kind: str = "walk_forward",
) -> int:
    """kind='walk_forward_custom' for user-windowed runs: the promotion gate never reads those."""
    capital = settings.cfg.paper.capital_tmn
    summary = selected_summary(wf, settings.cfg.promotion)
    with conn:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO backtest_runs (ts, kind, split_ts, history_from, history_to, grid_size, grids,"
            " spreads, selected, report_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                now_iso(),
                kind,
                wf.split_ts,
                wf.history_from,
                wf.history_to,
                wf.grid_size,
                dumps(wf.grids),
                dumps(wf.spreads),
                dumps(summary),
                str(report_path),
            ),
        )
        run_id = int(cur.lastrowid)
        rows = []
        for r in wf.results:
            sel = int(wf.selected.get(r.strategy) is r)
            for seg, res in (("IS", r.is_), ("OOS", r.oos)):
                s = stats(res.trades)
                rows.append(
                    (
                        run_id,
                        r.strategy,
                        r.timeframe,
                        "*",
                        dumps(r.params),
                        seg,
                        s.n,
                        str(s.net),
                        _pf(s),
                        f"{max_drawdown_pct(res.trades, capital):.3f}",
                        sel,
                    )
                )
                for mkt, ts in res.by_market().items():
                    ms = stats(ts)
                    rows.append(
                        (
                            run_id,
                            r.strategy,
                            r.timeframe,
                            mkt,
                            dumps(r.params),
                            seg,
                            ms.n,
                            str(ms.net),
                            _pf(ms),
                            f"{max_drawdown_pct(ts, capital):.3f}",
                            sel,
                        )
                    )
        conn.executemany(
            "INSERT INTO backtest_results (run_id, strategy, timeframe, market, params, segment, trades,"
            " net_tmn, pf, max_dd_pct, selected) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return run_id


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM backtest_runs WHERE kind='walk_forward' ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ---------------------------------------------------------------------------------- rendering


def _tier(fees: FeeTable, sym: str) -> str:
    f = fees.get(sym)
    return f"{f.maker * 100:.2f}/{f.taker * 100:.2f}" + ("*" if f.source == "default" else "")


def _rows(
    wf: WalkForward, fees: FeeTable, capital: Decimal, min_trades: int
) -> list[dict[str, Any]]:
    """Per strategy x timeframe (best IS params of that timeframe) x market, plus pooled rows."""
    out = []
    for name in STRATEGIES:
        for tf in wf.grids["timeframes"]:
            r = best_per_timeframe(wf, name, tf, min_trades)
            if r is None:
                continue
            is_valid = r.is_stats.n >= min_trades and (r.is_stats.profit_factor or ZERO) > 1
            is_m, oos_m = r.is_.by_market(), r.oos.by_market()
            for mkt in sorted(set(is_m) | set(oos_m)) + ["*"]:
                if mkt == "*":
                    it, ot = r.is_.trades, r.oos.trades
                else:
                    it, ot = is_m.get(mkt, []), oos_m.get(mkt, [])
                si, so = stats(it), stats(ot)
                out.append(
                    {
                        "strategy": name,
                        "tf": tf,
                        "market": "ALL (pooled, incl. USDT conversions)" if mkt == "*" else mkt,
                        "tier": "" if mkt == "*" else _tier(fees, mkt),
                        "params": ", ".join(f"{k}={v:g}" for k, v in r.params.items()),
                        "is": si,
                        "oos": so,
                        "is_dd": max_drawdown_pct(it, capital),
                        "oos_dd": max_drawdown_pct(ot, capital),
                        "selected": wf.selected.get(name) is r,
                        "is_valid": is_valid,
                        "pooled": mkt == "*",
                    }
                )
    return out


def render(
    wf: WalkForward, settings: Settings, fees: FeeTable, console: Console, run_id: int
) -> None:
    b, pc = settings.cfg.backtest, settings.cfg.promotion
    capital = settings.cfg.paper.capital_tmn
    console.print(
        f"[bold]Walk-forward run #{run_id}[/] · history {_d(wf.history_from)} → {_d(wf.history_to)} · "
        f"split {_d(wf.split_ts)} (IS {b.split_frac:.0%} / OOS {1 - b.split_frac:.0%}) · "
        f"[bold]grid size {wf.grid_size}[/] configs · {wf.seconds}s"
    )
    t = Table(
        header_style="bold cyan",
        title="In-sample vs out-of-sample (best IS params per strategy × timeframe)",
    )
    for col in (
        "Strategy",
        "TF",
        "Market",
        "Fee M/T %",
        "Params",
        "IS n",
        "IS net",
        "IS PF",
        "IS DD%",
        "OOS n",
        "OOS net",
        "OOS PF",
        "OOS DD%",
        "Sel",
    ):
        t.add_column(
            col,
            justify="right" if col not in ("Strategy", "Market", "Params") else "left",
            overflow="fold",
        )
    for row in _rows(wf, fees, capital, b.min_is_trades):
        si, so = row["is"], row["oos"]
        style = "bold" if row["pooled"] else ""
        t.add_row(
            row["strategy"],
            f"{row['tf']}m",
            row["market"],
            row["tier"],
            row["params"] + ("" if row["is_valid"] else " [dim](IS-invalid)[/]"),
            str(si.n),
            f"{si.net:,.0f}",
            _pf(si),
            f"{row['is_dd']:.2f}",
            str(so.n),
            f"{so.net:,.0f}",
            _pf(so),
            f"{row['oos_dd']:.2f}",
            "[green]✓[/]" if row["selected"] and row["pooled"] else "",
            style=style,
        )
    console.print(t)
    for name, s in selected_summary(wf, pc).items():
        if s["passes"]:
            console.print(
                f"[green]{name}: PASSES out-of-sample[/] ({s['selected']}, {s['reason']})"
            )
        else:
            console.print(f"[red]{name}: does NOT pass out-of-sample[/]: {s['reason']}")


def markdown(wf: WalkForward, settings: Settings, fees: FeeTable, run_id: int) -> str:
    b, pc = settings.cfg.backtest, settings.cfg.promotion
    capital = settings.cfg.paper.capital_tmn
    lines = [
        f"# Walk-forward run #{run_id}",
        "",
        f"- History: {_d(wf.history_from)} → {_d(wf.history_to)}; split at {_d(wf.split_ts)} "
        f"(oldest {b.split_frac:.0%} in-sample, newest {1 - b.split_frac:.0%} out-of-sample, same split for every timeframe).",
        f"- Grid: **{wf.grid_size} configurations** = mean_reversion {json.dumps(b.grid_mean_reversion)} "
        f"+ momentum {json.dumps(b.grid_momentum)}, each × timeframes {b.timeframes} min.",
        f"- Selection: in-sample only (best IS net PnL with ≥ {b.min_is_trades} IS trades and IS PF > 1). "
        f"Promotion gate: selected config's OOS stats only (≥ {pc.min_backtest_trades} trades, net > 0, PF > {pc.min_profit_factor}).",
        "- Fills: taker at bar close ± half the market's spread + "
        f"{settings.cfg.paper.slippage_bps} bps slippage; real fees from /v1/account/fee. Spreads: {json.dumps(wf.spreads)}.",
        "- Book imbalance (momentum) is not available historically: candle proxy (c − o)/(h − l).",
        "- 5/15-min bars are resampled from 1-min (the API has no 5/15 resolution); 60-min is native.",
        "- Max DD = peak-to-trough of 5M capital + cumulative realised net PnL (trade order).",
        "- basis_tmn_usdt is not candle-backtested (needs simultaneous books): shadow-evaluated, paper-only.",
        "",
        "| Strategy | TF | Market | Fee M/T % | Params | IS n | IS net | IS PF | IS DD% | OOS n | OOS net | OOS PF | OOS DD% | Selected |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in _rows(wf, fees, capital, b.min_is_trades):
        si, so = row["is"], row["oos"]
        lines.append(
            f"| {row['strategy']} | {row['tf']}m | {row['market']} | {row['tier']} | {row['params']}"
            f"{'' if row['is_valid'] else ' (IS-invalid)'} | {si.n} | {si.net:,.0f} | {_pf(si)} | {row['is_dd']:.2f} | "
            f"{so.n} | {so.net:,.0f} | {_pf(so)} | {row['oos_dd']:.2f} | {'✓' if row['selected'] and row['pooled'] else ''} |"
        )
    lines += [
        "",
        "## All configurations (pooled)",
        "",
        "| Strategy | TF | Params | IS n | IS net | IS PF | OOS n | OOS net | OOS PF |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in wf.results:
        si, so = r.is_stats, r.oos_stats
        lines.append(
            f"| {r.strategy} | {r.timeframe}m | {', '.join(f'{k}={v:g}' for k, v in r.params.items())} | "
            f"{si.n} | {si.net:,.0f} | {_pf(si)} | {so.n} | {so.net:,.0f} | {_pf(so)} |"
        )
    lines += ["", "## Verdict", ""]
    for name, s in selected_summary(wf, pc).items():
        verdict = "PASSES out-of-sample" if s["passes"] else "does NOT pass out-of-sample"
        lines.append(
            f"- **{name}**: {verdict}: {s['reason']}"
            + (f" (selected {s['selected']})" if s.get("selected") else "")
        )
    return "\n".join(lines) + "\n"
