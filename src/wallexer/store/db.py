"""SQLite store (WAL). Schema for every table in architecture.md §7.

Money/quantity columns are TEXT holding Decimal strings: SQLite REAL would round-trip through
binary float. JSON blobs (features, reasons, payloads) are TEXT holding JSON.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    ts          INTEGER NOT NULL,          -- unix seconds, candle open
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
    volume      TEXT NOT NULL,
    PRIMARY KEY (symbol, resolution, ts)
);

CREATE TABLE IF NOT EXISTS depth_snapshots (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    best_bid    TEXT, best_ask TEXT,
    bids        TEXT NOT NULL,             -- JSON [[price, qty], ...]
    asks        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_depth_symbol_ts ON depth_snapshots(symbol, ts);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL,             -- backtest | shadow | trade
    run_id      TEXT,
    symbol      TEXT,
    strategy    TEXT,
    features    TEXT NOT NULL DEFAULT '{}',
    signal      TEXT NOT NULL DEFAULT '{}',
    weights     TEXT NOT NULL DEFAULT '{}',
    risk_checks TEXT NOT NULL DEFAULT '{}',
    action      TEXT NOT NULL,             -- e.g. buy | sell | hold | reject
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),   -- no decision → no order
    client_id   TEXT NOT NULL UNIQUE,
    mode        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    type        TEXT NOT NULL,
    price       TEXT,
    quantity    TEXT NOT NULL,
    status      TEXT NOT NULL,
    executed_qty TEXT NOT NULL DEFAULT '0',
    executed_price TEXT,
    fee         TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_decision ON orders(decision_id);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    ts          TEXT NOT NULL,
    price       TEXT NOT NULL,
    quantity    TEXT NOT NULL,
    fee         TEXT NOT NULL DEFAULT '0',
    fee_asset   TEXT,
    is_maker    INTEGER
);

CREATE TABLE IF NOT EXISTS strategy_paper_pnl (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    run_id      TEXT,
    strategy    TEXT NOT NULL,
    equity      TEXT NOT NULL,
    realized    TEXT NOT NULL,
    unrealized  TEXT NOT NULL,
    fees        TEXT NOT NULL,
    drawdown_pct TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_strategy_ts ON strategy_paper_pnl(strategy, ts);

-- Closed paper trades per strategy (needed by the promotion gate; addition to arch §7).
-- source: backtest (startup replay) | shadow (live-data paper). kind: trade | cycle | conversion.
CREATE TABLE IF NOT EXISTS paper_trades (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT,
    strategy    TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    market      TEXT NOT NULL,
    qty         TEXT NOT NULL,
    entry_ts    TEXT NOT NULL,
    exit_ts     TEXT NOT NULL,
    entry_px    TEXT NOT NULL,
    exit_px     TEXT NOT NULL,
    fees_tmn    TEXT NOT NULL,
    gross_tmn   TEXT NOT NULL,
    net_tmn     TEXT NOT NULL,
    entry_reason TEXT NOT NULL DEFAULT '{}',
    exit_reason TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_paper_trades_strategy ON paper_trades(strategy, source, exit_ts);

CREATE TABLE IF NOT EXISTS allocations (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    run_id      TEXT,
    baseline    TEXT NOT NULL,             -- JSON weights from the quant allocator
    agent       TEXT,                      -- JSON weights proposed by the LLM (NULL on fallback)
    final       TEXT NOT NULL,             -- JSON weights after guardrail clamping
    source      TEXT NOT NULL,             -- agent | fallback | baseline
    llm_call_id INTEGER REFERENCES llm_calls(id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    purpose     TEXT NOT NULL,             -- allocate | exp_reason | autoset | doctor
    model       TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    request     TEXT NOT NULL,
    response    TEXT,
    parsed      TEXT,
    latency_ms  INTEGER,
    fallback    INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_prompt_hash ON llm_calls(prompt_hash);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL,
    run_id      TEXT,
    equity_tmn  TEXT NOT NULL,
    tmn         TEXT NOT NULL,
    exposure_pct TEXT NOT NULL,
    drawdown_pct TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_curve(ts);

-- Incremental history ledger: a complete past chunk is never fetched again.
CREATE TABLE IF NOT EXISTS history_chunks (
    symbol      TEXT NOT NULL,
    resolution  TEXT NOT NULL,             -- native: 1 | 60
    chunk_start INTEGER NOT NULL,          -- unix seconds
    chunk_end   INTEGER NOT NULL,
    bars        INTEGER NOT NULL,
    complete    INTEGER NOT NULL,          -- 0 for the chunk that includes "now"
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, resolution, chunk_start)
);

-- Walk-forward runs (tune in-sample, judge out-of-sample) and their per-row results.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- walk_forward
    split_ts    INTEGER NOT NULL,
    history_from INTEGER NOT NULL,
    history_to  INTEGER NOT NULL,
    grid_size   INTEGER NOT NULL,
    grids       TEXT NOT NULL,             -- JSON: the parameter grids evaluated
    spreads     TEXT NOT NULL,             -- JSON: spread bps per market + source
    selected    TEXT NOT NULL,             -- JSON: per strategy, chosen timeframe/params + OOS stats
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS backtest_results (
    run_id      INTEGER NOT NULL REFERENCES backtest_runs(id),
    strategy    TEXT NOT NULL,
    timeframe   INTEGER NOT NULL,
    market      TEXT NOT NULL,             -- symbol, or '*' for the pooled book
    params      TEXT NOT NULL,
    segment     TEXT NOT NULL,             -- IS | OOS
    trades      INTEGER NOT NULL,
    net_tmn     TEXT NOT NULL,
    pf          TEXT,
    max_dd_pct  TEXT NOT NULL,
    selected    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bt_results_run ON backtest_results(run_id, strategy);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- e.g. risk.order_queue_backlog, exchange.min_order_learned
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts);
"""


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN where needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "PRAGMA busy_timeout=30000"
    )  # several processes (engine, CLI, research) share it  # CLI and engine share the file
    # WAL + NORMAL: no fsync per commit (only at checkpoints). A crash can lose the last few
    # commits but never corrupts the DB; FULL made per-row candle inserts take minutes.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


def log_event(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)", (now_iso(), kind, dumps(payload))
    )


def event_sink(conn: sqlite3.Connection):
    """Adapter so exchange-layer code can log events without importing the store."""

    def sink(kind: str, payload: dict[str, Any]) -> None:
        log_event(conn, kind, payload)

    return sink
