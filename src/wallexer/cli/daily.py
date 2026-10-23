"""`sum [date]`: a daily market + agent summary.

Part 1 (facts) is static and computed from the store only: candles, depth/agent-prompt spreads,
regime percentiles (allocations._regime), feed events, walk-forward status, agent cycles,
orders and paper trades. `daily_facts(conn, day)` is pure and read-only, so the engine's
read-only API serves it as /api/sum?date=.

Part 2 (explanation) is one LLM call on the anonymized facts plus that day's agent rationales,
with a fixed output format. Numbers in the answer are checked against the input; unknown cited
decision ids are dropped. Results are cached per (date, origin): a past day is summarized once,
today is regenerated, --refresh forces a new one.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import statistics
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from rich.console import Console
from rich.panel import Panel

from wallexer.cli.theme import bps, pct, table, tmn
from wallexer.config import Settings

AGENT_ACTIONS = ("agent_decision", "agent_fallback")


def _tehran() -> Any:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Tehran")
    except Exception:  # slim images may lack tzdata; Iran has no DST since 2022
        return timezone(timedelta(hours=3, minutes=30), "IRST")


TEHRAN = _tehran()


class DateError(ValueError):
    """Bad `sum` date argument."""


def parse_day(arg: str | None, now: datetime | None = None) -> date:
    today = (now or datetime.now(UTC)).date()
    a = (arg or "today").strip().lower()
    if a == "today":
        return today
    if a == "yesterday":
        return today - timedelta(days=1)
    try:
        d = date.fromisoformat(a)
    except ValueError:
        raise DateError(f"bad date {arg!r}: use YYYY-MM-DD, today or yesterday") from None
    if d > today:
        raise DateError(f"{d} is in the future")
    return d


def _j(s: str | None) -> Any:
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None or math.isnan(x) else round(x, n)


def _pctiles(xs: list[float]) -> dict[str, float] | None:
    if not xs:
        return None
    return {"min": _r(min(xs), 3), "median": _r(statistics.median(xs), 3), "max": _r(max(xs), 3)}


def daily_facts(conn: sqlite3.Connection, day: date, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = min(start + timedelta(days=1), now) if day == now.date() else start + timedelta(days=1)
    lo, hi = start.isoformat(), end.isoformat()
    lo_ts, hi_ts = int(start.timestamp()), int(end.timestamp())

    # --- markets: symbols and taker fees from the agent's own prompts that day, else candles
    prompt_mkts: dict[str, list[dict[str, Any]]] = {}
    for (req,) in conn.execute(
        "SELECT request FROM llm_calls WHERE purpose='allocate' AND ts >= ? AND ts < ? ORDER BY id",
        (lo, hi),
    ):
        msgs = _j(req) or []
        user = next((m.get("content") for m in msgs if m.get("role") == "user"), None)
        for m in (_j(user) or {}).get("markets") or []:
            prompt_mkts.setdefault(m["market"], []).append(m)
    symbols = sorted(prompt_mkts) or [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT symbol FROM candles WHERE resolution='1' AND ts >= ? AND ts < ?"
            " ORDER BY symbol",
            (lo_ts, hi_ts),
        )
    ]
    markets = []
    for sym in symbols:
        bars = conn.execute(
            "SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND resolution='1'"
            " AND ts >= ? AND ts < ? ORDER BY ts",
            (sym, lo_ts, hi_ts),
        ).fetchall()
        row: dict[str, Any] = {"market": sym, "bars_1m": len(bars)}
        if bars:
            o, c = float(bars[0]["open"]), float(bars[-1]["close"])
            closes = [float(b["close"]) for b in bars]
            rets = [
                math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0
            ]
            row |= {
                "open": o,
                "high": max(float(b["high"]) for b in bars),
                "low": min(float(b["low"]) for b in bars),
                "close": c,
                "change_pct": _r((c - o) / o * 100 if o else None, 3),
                "realized_vol_pct": _r(math.sqrt(sum(r * r for r in rets)) * 100, 3),
                "coverage_pct": _r(len(bars) / max(1, (hi_ts - lo_ts) / 60) * 100, 1),
            }
        spreads = [
            (float(a) - float(b)) / ((float(a) + float(b)) / 2) * 1e4
            for b, a in conn.execute(
                "SELECT best_bid, best_ask FROM depth_snapshots WHERE symbol=? AND ts >= ? AND ts < ?"
                " AND best_bid IS NOT NULL AND best_ask IS NOT NULL",
                (sym, lo, hi),
            )
            if float(a) > 0 and float(b) > 0
        ]
        src = "order books"
        if not spreads:
            spreads = [
                float(m["spread_bps"])
                for m in prompt_mkts.get(sym, [])
                if m.get("spread_bps") not in (None, "–")
            ]
            src = "agent snapshots"
        taker = None
        for m in reversed(prompt_mkts.get(sym, [])):
            with contextlib.suppress(KeyError, ValueError, TypeError):
                taker = float(str(m["taker_fee"]).rstrip("%")) / 100
                break
        med = statistics.median(spreads) if spreads else None
        row |= {
            "spread_median_bps": _r(med, 1),
            "spread_samples": len(spreads),
            "spread_source": src if spreads else None,
            "taker_fee_pct": _r(taker * 100, 3) if taker is not None else None,
            "round_trip_bps": _r(2 * taker * 1e4 + med, 1)
            if taker is not None and med is not None
            else None,
        }
        markets.append(row)

    # --- regime percentiles (allocator input, every alloc cycle)
    reg: dict[str, list[float]] = {"trend": [], "range": [], "high_vol": []}
    for (b,) in conn.execute("SELECT baseline FROM allocations WHERE ts >= ? AND ts < ?", (lo, hi)):
        r = (_j(b) or {}).get("_regime") or {}
        for k in reg:
            if isinstance(r.get(k), int | float):
                reg[k].append(float(r[k]))
    regime = {k: _pctiles(v) for k, v in reg.items() if v}

    # --- feed / engine events
    feed: dict[str, int] = {}
    feed_by_market: dict[str, int] = {}
    engine: dict[str, int] = {}
    for kind, payload in conn.execute(
        "SELECT kind, payload FROM events WHERE ts >= ? AND ts < ?", (lo, hi)
    ):
        if kind.startswith("feed."):
            feed[kind] = feed.get(kind, 0) + 1
            sym = (_j(payload) or {}).get("symbol")
            if sym:
                feed_by_market[sym] = feed_by_market.get(sym, 0) + 1
        elif kind.startswith(("engine.", "control.", "risk.")):
            engine[kind] = engine.get(kind, 0) + 1

    # --- walk-forward / promotion as of the end of the day
    wf_runs = conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE kind='walk_forward' AND ts >= ? AND ts < ?",
        (lo, hi),
    ).fetchone()[0]
    last = conn.execute(
        "SELECT id, ts, selected FROM backtest_runs WHERE kind='walk_forward' AND ts < ? ORDER BY id DESC LIMIT 1",
        (hi,),
    ).fetchone()
    walk_forward = None
    if last:
        sel = _j(last["selected"]) or {}
        walk_forward = {
            "run_id": last["id"],
            "ts": last["ts"][:16],
            "runs_this_day": wf_runs,
            "strategies": {
                k: {"passes": v.get("passes"), "reason": v.get("reason")} for k, v in sel.items()
            },
            "promoted": sorted(k for k, v in sel.items() if v.get("passes")),
        }

    # --- agent cycles
    cycles = conn.execute(
        f"SELECT id, ts, action, signal, reason FROM decisions WHERE action IN {AGENT_ACTIONS}"
        " AND ts >= ? AND ts < ? ORDER BY id",
        (lo, hi),
    ).fetchall()
    stances: dict[str, int] = {}
    rms: list[float] = []
    pauses: set[str] = set()
    watch: list[str] = []
    rationales = []
    fallbacks = 0
    for c in cycles:
        if c["action"] == "agent_fallback":
            fallbacks += 1
            continue
        d = (_j(c["signal"]) or {}).get("decision") or {}
        st = d.get("stance") or "?"
        stances[st] = stances.get(st, 0) + 1
        if isinstance(d.get("risk_multiplier"), int | float):
            rms.append(float(d["risk_multiplier"]))
        pauses |= set(d.get("pause_markets") or [])
        for w in d.get("watch_requests") or []:
            watch.append(
                f"{w.get('action')} {w.get('strategy')}@{w.get('timeframe')}m {w.get('market')}"
            )
        rationales.append(
            {
                "decision_id": c["id"],
                "time_utc": c["ts"][11:16],
                "stance": st,
                "rationale": d.get("rationale") or c["reason"],
            }
        )
    agent = {
        "cycles": len(cycles),
        "fallbacks": fallbacks,
        "stances": stances,
        "risk_multiplier": _pctiles(rms),
        "risk_multipliers_seen": sorted(set(rms)),
        "paused_markets": sorted(pauses),
        "watch_requests": {k: watch.count(k) for k in sorted(set(watch))},
        "decision_ids": [c["id"] for c in cycles],
        "first_decision_id": cycles[0]["id"] if cycles else None,
        "last_decision_id": cycles[-1]["id"] if cycles else None,
    }

    # --- orders, paper trades, equity
    orders = conn.execute(
        "SELECT mode, status, COUNT(*) n FROM orders WHERE created_at >= ? AND created_at < ? GROUP BY mode, status",
        (lo, hi),
    ).fetchall()
    paper = conn.execute(
        "SELECT source, COUNT(*) n, SUM(CAST(net_tmn AS REAL)) net, SUM(CAST(fees_tmn AS REAL)) fees"
        " FROM paper_trades WHERE kind != 'conversion' AND exit_ts >= ? AND exit_ts < ? GROUP BY source",
        (lo, hi),
    ).fetchall()
    equity = {}
    for mode in ("shadow", "trade"):
        pts = [
            float(r[0])
            for r in conn.execute(
                "SELECT equity_tmn FROM equity_curve WHERE mode=? AND ts >= ? AND ts < ? ORDER BY rowid",
                (mode, lo, hi),
            )
        ]
        if pts:
            peak, worst = pts[0], 0.0
            for x in pts:
                peak = max(peak, x)
                worst = max(worst, (peak - x) / peak * 100 if peak else 0.0)
            equity[mode] = {
                "points": len(pts),
                "start_tmn": pts[0],
                "end_tmn": pts[-1],
                "change_pct": _r((pts[-1] - pts[0]) / pts[0] * 100 if pts[0] else 0.0, 3),
                "max_drawdown_pct": _r(worst, 3),
            }

    has_data = bool(any(m.get("bars_1m") for m in markets) or cycles or orders or paper or equity)
    return {
        "date": day.isoformat(),
        "window_utc": [lo[:16], hi[:16]],
        "partial_day": day == now.date(),
        "has_data": has_data,
        "markets": markets,
        "regime_percentiles": regime,
        "feed_events": feed,
        "feed_events_by_market": feed_by_market,
        "engine_events": engine,
        "walk_forward": walk_forward,
        "agent": agent,
        "agent_rationales": rationales,
        "orders": [dict(o) for o in orders],
        "paper_trades": [dict(p) for p in paper],
        "equity": equity,
    }


# ---------------------------------------------------------------------------------- Part 2

SUM_SYSTEM = """You write the daily report of an automated spot-crypto trading bot for a hackathon judge.
Input: one day's static facts (market moves, volatility, spreads and costs in bps/%, regime percentiles,
feed problems, walk-forward/promotion status, the LLM agent's cycles) and the agent's own rationales.
Rules: use ONLY numbers that appear in the input, copied as given; never compute new figures, never
invent prices, balances or events. When you refer to an agent decision, cite its decision_id like
(#123) and list every cited id in cited_decision_ids. Plain sentences, no markdown.
Fields: what_the_market_did (3-5 sentences: moves, volatility vs round-trip cost, feed health),
what_the_agent_decided_and_why (stances, risk multipliers, pauses, watch requests, the reasons it gave),
what_would_change_its_mind (the concrete conditions in the input that would lead to entries:
promotion gate, costs vs volatility, feed health), confidence (low|medium|high in the agent's stance)."""


def anonymize_facts(f: dict[str, Any], capital: float) -> dict[str, Any]:
    """Keep %, bps, counts, public prices and decision ids; drop balances and run ids."""
    out = json.loads(json.dumps(f))
    for mode, e in (out.get("equity") or {}).items():
        out["equity"][mode] = {
            "change_pct": e["change_pct"],
            "max_drawdown_pct": e["max_drawdown_pct"],
            "points": e["points"],
        }
    for p in out.get("paper_trades") or []:
        p["net_pct_of_capital"] = _r((p.pop("net") or 0) / capital * 100, 4)
        p["fees_pct_of_capital"] = _r((p.pop("fees") or 0) / capital * 100, 4)
    if out.get("walk_forward"):
        out["walk_forward"].pop("run_id", None)
    out.pop("agent_rationales", None)
    return out


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\b\d{1,2}:\d{2}\b|#\d+")
_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    return _NUM_RE.findall(_DATE_RE.sub(" ", text.replace(",", "")))


def unknown_numbers(answer: str, source: str) -> list[str]:
    """Numbers in the answer that do not appear in the input (at the precision written)."""
    have = [float(x) for x in _numbers(source)]
    bad = []
    for s in _numbers(answer):
        x = float(s)
        tol = 0.5 * 10 ** -(len(s.split(".")[1]) if "." in s else 0)
        if not any(abs(abs(x) - abs(y)) <= tol for y in have) and s not in bad:
            bad.append(s)
    return bad


SUM_FIELDS = {
    "what_the_market_did": "What the market did",
    "what_the_agent_decided_and_why": "What the agent decided and why",
    "what_would_change_its_mind": "What would change its mind",
}


async def explain(
    settings: Settings,
    conn: sqlite3.Connection,
    facts: dict[str, Any],
    on_text: Any = None,
) -> tuple[dict[str, Any] | None, int | None, str | None]:
    from wallexer.agent.client import LlmClient
    from wallexer.agent.schema import DailySummary, daily_summary_schema

    payload = {
        "date": facts["date"],
        "partial_day": facts["partial_day"],
        "facts": anonymize_facts(facts, float(settings.cfg.paper.capital_tmn)),
        "agent_rationales": facts.get("agent_rationales") or [],
    }
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    client = LlmClient(settings, conn)
    try:
        res = await client.structured(
            purpose="daily_summary",
            system=SUM_SYSTEM,
            user=user,
            schema=daily_summary_schema(),
            model=DailySummary,
            schema_name="daily_summary",
            on_text=on_text,
        )
    finally:
        await client.close()
    if res.parsed is None:
        return None, res.llm_call_id, res.error or "LLM unavailable"
    e = res.parsed.model_dump()
    known = set(facts["agent"]["decision_ids"])
    e["dropped_decision_ids"] = [i for i in e["cited_decision_ids"] if i not in known]
    e["cited_decision_ids"] = [i for i in e["cited_decision_ids"] if i in known]
    text = " ".join(
        str(e[k])
        for k in (
            "what_the_market_did",
            "what_the_agent_decided_and_why",
            "what_would_change_its_mind",
        )
    )
    e["numbers_not_in_input"] = unknown_numbers(text, user)
    e["source"] = res.source
    return e, res.llm_call_id, None


# ---------------------------------------------------------------------------------- command


def store_summary(
    conn: sqlite3.Connection,
    day: date,
    origin: str,
    facts: dict[str, Any],
    explanation: dict[str, Any] | None,
    call_id: int | None,
) -> None:
    from wallexer.store.db import now_iso

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_summaries (date, origin, created_at, facts, explanation,"
            " llm_call_id) VALUES (?,?,?,?,?,?)",
            (
                day.isoformat(),
                origin,
                now_iso(),
                json.dumps(facts, default=str),
                json.dumps(explanation) if explanation else None,
                call_id,
            ),
        )


async def run_sum(
    settings: Settings, console: Console, arg: str | None, refresh: bool, full: bool = False
) -> int:
    from wallexer.cli import source, stream
    from wallexer.store.db import init_db

    day = parse_day(arg)
    today = day == datetime.now(UTC).date()
    r = source.remote(settings)
    origin = r.url if r else "LOCAL"
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        cached = conn.execute(
            "SELECT * FROM daily_summaries WHERE date=? AND origin=?", (day.isoformat(), origin)
        ).fetchone()
        if cached and cached["explanation"] and not today and not refresh:
            render(
                json.loads(cached["facts"]),
                json.loads(cached["explanation"]),
                console,
                note=f"cached {cached['created_at'][:16]} UTC · --refresh to regenerate",
                full=full,
            )
            return 0
        explanation = None
        if r:
            data = source.api_get(r, "/api/sum", {"date": day.isoformat()}) or {}
            facts = data.get("facts")
            # The server's own answer (the demo regenerates today's hourly), unless --refresh
            # asks for a fresh one with the local key.
            if not refresh:
                explanation = data.get("explanation")
        else:
            facts = daily_facts(conn, day)
        if not facts or not facts["has_data"]:
            console.print(
                f"No data for {day} ({'remote ' + origin if r else 'local store'}): no candles, agent "
                "cycles, orders or paper trades were recorded that day."
            )
            return 1
        call_id, err = None, None
        from_server = explanation is not None
        if explanation is None:
            key = settings.secrets.boxapi_api_key
            if r and not (key and key.get_secret_value()):
                err = "no explanation on the server yet, and no local BoxAPI key (`wallexer setup`)"
            else:
                with (
                    console.status("Asking the LLM for the explanation (anonymized facts)…"),
                    stream.block(f"Summary of {day}", SUM_FIELDS) as on_text,
                ):
                    explanation, call_id, err = await explain(settings, conn, facts, on_text)
        store_summary(conn, day, origin, facts, explanation, call_id)
    finally:
        conn.close()
    note = (
        "explanation from the server" + (" (today: regenerated hourly)" if today else "")
        if from_server
        else "today so far: regenerated on every run"
        if today
        else "generated now"
    )
    render(facts, explanation, console, note=note, error=err, llm_call_id=call_id, full=full)
    return 0


# ---------------------------------------------------------------------------------- rendering


def _teh(iso_utc: str) -> str:
    return (
        datetime.fromisoformat(iso_utc)
        .replace(tzinfo=UTC)
        .astimezone(TEHRAN)
        .strftime("%m-%d %H:%M")
    )


def render(
    f: dict[str, Any],
    e: dict[str, Any] | None,
    console: Console,
    *,
    note: str = "",
    error: str | None = None,
    llm_call_id: int | None = None,
    full: bool = False,
) -> None:
    if not full:
        render_brief(f, e, console, note=note, error=error)
        return
    lo, hi = f["window_utc"]
    console.print(
        f"[key]Daily summary {f['date']}[/] · UTC {lo[11:16]}–{hi[11:16] if f['partial_day'] else '24:00'}"
        f" · Tehran {_teh(lo)} → {_teh(hi)}{' (so far)' if f['partial_day'] else ''} · [muted]{note}[/]"
    )
    console.rule("[head]Part 1 · facts (static)[/]", align="left")
    t = table(
        "Markets",
        [
            ("Market", "text"),
            ("Open", "num"),
            ("High", "num"),
            ("Low", "num"),
            ("Close", "num"),
            ("Change", "num"),
            ("Real. vol", "num"),
            ("Spread med bps", "num"),
            ("Round trip bps", "num"),
            ("Coverage", "num"),
        ],
    )
    for m in f["markets"]:
        chg = m.get("change_pct")
        style = "ok" if chg and chg > 0 else "err" if chg and chg < 0 else ""
        t.add_row(
            m["market"],
            _px(m.get("open")),
            _px(m.get("high")),
            _px(m.get("low")),
            _px(m.get("close")),
            f"[{style}]{pct(chg, 2)}[/]" if style else pct(chg, 2),
            pct(m.get("realized_vol_pct"), 2, signed=False),
            bps(m.get("spread_median_bps")),
            bps(m.get("round_trip_bps")),
            pct(m.get("coverage_pct"), 0, signed=False),
        )
    console.print(t)
    srcs = sorted({m["spread_source"] for m in f["markets"] if m.get("spread_source")})
    console.print(
        f"[muted]realized vol = √Σ(1-min log returns²) over the day · spread from {', '.join(srcs) or 'n/a'} · "
        "round trip = 2 × taker + median spread[/]"
    )
    rg = f.get("regime_percentiles") or {}
    console.print(
        "[key]Regime percentiles[/] (vs 30d history, min / median / max): "
        + (
            " · ".join(f"{k} {v['min']} / {v['median']} / {v['max']}" for k, v in rg.items())
            or "none recorded"
        )
    )
    fe = f.get("feed_events") or {}
    console.print(
        "[key]Feed events[/]: "
        + (", ".join(f"{k} {v}" for k, v in fe.items()) or "[ok]none[/]")
        + (f" · by market {f['feed_events_by_market']}" if f.get("feed_events_by_market") else "")
    )
    wf = f.get("walk_forward")
    if wf:
        promoted = ", ".join(wf["promoted"]) or "none"
        reasons = "; ".join(
            f"{k}: {'PASS' if v['passes'] else 'FAIL'} ({v['reason']})"
            for k, v in wf["strategies"].items()
        )
        console.print(
            f"[key]Walk-forward[/] run #{wf['run_id']} ({wf['ts']} UTC), {wf['runs_this_day']} run(s) this day · "
            f"promoted: {promoted}\n[muted]{reasons}[/]"
        )
    else:
        console.print("[key]Walk-forward[/]: no run yet")
    a = f["agent"]
    rm = a.get("risk_multiplier")
    console.print(
        f"[key]Agent[/]: {a['cycles']} cycles (#{a['first_decision_id']}–#{a['last_decision_id']}) · stances {a['stances'] or '{}'} · "
        f"risk multiplier {('min ' + str(rm['min']) + ' / max ' + str(rm['max'])) if rm else '–'} · fallbacks {a['fallbacks']}\n"
        f"  paused: {', '.join(a['paused_markets']) or 'none'}\n"
        f"  watch requests: {_top(a['watch_requests'])}"
    )
    od = ", ".join(f"{o['mode']} {o['status']} {o['n']}" for o in f["orders"]) or "none"
    pt = (
        ", ".join(
            f"{p['source']} {p['n']} trades, net {tmn(p['net'], signed=True)} TMN"
            for p in f["paper_trades"]
        )
        or "none"
    )
    eq = (
        " · ".join(
            f"{m} equity {pct(v['change_pct'], 3)} (max DD {pct(v['max_drawdown_pct'], 3, signed=False)})"
            for m, v in (f.get("equity") or {}).items()
        )
        or "no equity points"
    )
    console.print(f"[key]Orders[/]: {od} · [key]Paper trades[/]: {pt} · {eq}")

    console.rule("[head]Part 2 · explanation (LLM, anonymized input)[/]", align="left")
    if e is None:
        console.print(
            f"[err]LLM explanation unavailable[/] ({error}). Part 1 above is complete; retry with `wallexer sum {f['date']} --refresh`."
        )
        return
    color = {"low": "err", "medium": "warn", "high": "ok"}[e["confidence"]]
    cites = ", ".join(f"#{i}" for i in e["cited_decision_ids"]) or "none"
    body = (
        f"[key]What the market did[/]\n{e['what_the_market_did']}\n\n"
        f"[key]What the agent decided and why[/]\n{e['what_the_agent_decided_and_why']}\n\n"
        f"[key]What would change its mind[/]\n{e['what_would_change_its_mind']}\n\n"
        f"[key]Confidence[/] [{color}]{e['confidence']}[/] · cited decisions {cites}"
    )
    src = f"{e.get('source', 'llm')}" + (f", llm_call #{llm_call_id}" if llm_call_id else "")
    console.print(Panel(body, title=f"Daily explanation {f['date']} ({src})", expand=False))
    if e.get("numbers_not_in_input"):
        console.print(
            f"[warn]check: numbers not found in the input: {', '.join(e['numbers_not_in_input'])}[/]"
        )
    if e.get("dropped_decision_ids"):
        console.print(f"[warn]dropped unknown cited decision ids: {e['dropped_decision_ids']}[/]")


def render_brief(
    f: dict[str, Any], e: dict[str, Any] | None, console: Console, *, note: str, error: str | None
) -> None:
    """Default `sum`: one verdict panel (what the agent decided and why) + a short market table."""
    a = f["agent"]
    console.print(
        f"[key]Daily summary {f['date']}[/]{' (so far)' if f['partial_day'] else ''} · "
        f"agent {a['cycles']} cycles, stances {a['stances'] or '{}'} · [muted]{note}[/]"
    )
    t = table(None, [("Market", "text"), ("Close", "num"), ("Change", "num"), ("Real. vol", "num")])
    for m in f["markets"]:
        chg = m.get("change_pct")
        style = "ok" if chg and chg > 0 else "err" if chg and chg < 0 else ""
        t.add_row(
            m["market"],
            _px(m.get("close")),
            f"[{style}]{pct(chg, 2)}[/]" if style else pct(chg, 2),
            pct(m.get("realized_vol_pct"), 2, signed=False),
        )
    console.print(t)
    if e is None:
        console.print(f"[err]LLM explanation unavailable[/] ({error})")
    else:
        color = {"low": "err", "medium": "warn", "high": "ok"}[e["confidence"]]
        cites = ", ".join(f"#{i}" for i in e["cited_decision_ids"]) or "none"
        console.print(
            Panel(
                f"{e['what_the_agent_decided_and_why']}\n\n"
                f"[key]Confidence[/] [{color}]{e['confidence']}[/] · cited decisions {cites}",
                title="What the agent decided and why",
                expand=False,
            )
        )
    console.print(
        "[muted]`sum --full`: OHLC, spreads, regime, feed events, what the market did, "
        "what would change its mind[/]"
    )


def _top(counts: dict[str, int], n: int = 5) -> str:
    if not counts:
        return "none"
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    more = f" … +{len(items) - n} more" if len(items) > n else ""
    return ", ".join(f"{k} ×{v}" for k, v in items[:n]) + more


def _px(v: Any) -> str:
    if v is None:
        return "–"
    v = float(v)
    if v >= 1e7:  # TMN-quoted coins: 22.45B reads better than a 14-digit number
        for unit, size in (("B", 1e9), ("M", 1e6)):
            if v >= size:
                return f"{v / size:,.2f}{unit}"
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.4f}".rstrip("0").rstrip(".")
