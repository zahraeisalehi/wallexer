"""Repositories: the only place that writes rows. Decimals are stored as strings."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from wallexer.store.db import dumps, now_iso


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


def record_decision(
    conn: sqlite3.Connection,
    *,
    mode: str,
    action: str,
    run_id: str | None = None,
    symbol: str | None = None,
    strategy: str | None = None,
    features: Any = None,
    signal: Any = None,
    weights: Any = None,
    risk_checks: Any = None,
    reason: str = "",
) -> int:
    """Write the decision row. Must happen BEFORE any order it leads to (orders FK to it)."""
    cur = conn.execute(
        "INSERT INTO decisions (ts, mode, run_id, symbol, strategy, features, signal, weights,"
        " risk_checks, action, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            mode,
            run_id,
            symbol,
            strategy,
            dumps(features or {}),
            dumps(signal or {}),
            dumps(weights or {}),
            dumps(risk_checks or {}),
            action,
            reason,
        ),
    )
    return int(cur.lastrowid)


def insert_order(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    client_id: str,
    mode: str,
    symbol: str,
    side: str,
    type_: str,
    price: Decimal | None,
    quantity: Decimal,
    status: str,
) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO orders (decision_id, client_id, mode, symbol, side, type, price, quantity,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id,
            client_id,
            mode,
            symbol,
            side,
            type_,
            _s(price),
            _s(quantity),
            status,
            ts,
            ts,
        ),
    )
    return int(cur.lastrowid)


def update_order(
    conn: sqlite3.Connection,
    client_id: str,
    *,
    status: str,
    executed_qty: Decimal | None = None,
    executed_price: Decimal | None = None,
    fee: Decimal | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE orders SET status=?, executed_qty=COALESCE(?, executed_qty),"
        " executed_price=COALESCE(?, executed_price), fee=COALESCE(?, fee),"
        " error=COALESCE(?, error), updated_at=? WHERE client_id=?",
        (status, _s(executed_qty), _s(executed_price), _s(fee), error, now_iso(), client_id),
    )


def insert_fill(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
    fee_asset: str | None,
    is_maker: bool | None,
) -> None:
    row = conn.execute("SELECT id FROM orders WHERE client_id=?", (client_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO fills (order_id, ts, price, quantity, fee, fee_asset, is_maker)"
        " VALUES (?,?,?,?,?,?,?)",
        (row["id"], now_iso(), _s(price), _s(quantity), _s(fee), fee_asset, is_maker),
    )


def has_live_history(conn: sqlite3.Connection) -> bool:
    """True once any trade-mode decision or order exists (then the start is not the 'first')."""
    for table in ("decisions", "orders"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE mode='trade' LIMIT 1").fetchone():
            return True
    return False


def insert_paper_trade(conn: sqlite3.Connection, t: Any, run_id: str | None) -> None:
    conn.execute(
        "INSERT INTO paper_trades (run_id, strategy, source, kind, market, qty, entry_ts, exit_ts,"
        " entry_px, exit_px, fees_tmn, gross_tmn, net_tmn, entry_reason, exit_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            t.strategy,
            t.source,
            t.kind,
            t.market,
            _s(t.qty),
            t.entry_ts,
            t.exit_ts,
            _s(t.entry_px),
            _s(t.exit_px),
            _s(t.fees_tmn),
            _s(t.gross_tmn),
            _s(t.net_tmn),
            dumps(t.entry_reason),
            dumps(t.exit_reason),
        ),
    )


def insert_paper_equity(
    conn: sqlite3.Connection,
    *,
    run_id: str | None,
    strategy: str,
    equity: Decimal,
    realized: Decimal,
    unrealized: Decimal,
    fees: Decimal,
    drawdown_pct: Decimal,
) -> None:
    conn.execute(
        "INSERT INTO strategy_paper_pnl (ts, run_id, strategy, equity, realized, unrealized, fees,"
        " drawdown_pct) VALUES (?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            run_id,
            strategy,
            _s(equity),
            _s(realized),
            _s(unrealized),
            _s(fees),
            _s(drawdown_pct),
        ),
    )


def insert_allocation(
    conn: sqlite3.Connection,
    *,
    run_id: str | None,
    baseline: dict[str, Any],
    agent: dict[str, Any] | None,
    final: dict[str, Any],
    source: str,
) -> None:
    conn.execute(
        "INSERT INTO allocations (ts, run_id, baseline, agent, final, source) VALUES (?,?,?,?,?,?)",
        (
            now_iso(),
            run_id,
            dumps(baseline),
            dumps(agent) if agent is not None else None,
            dumps(final),
            source,
        ),
    )


def insert_equity(
    conn: sqlite3.Connection,
    *,
    mode: str,
    run_id: str | None,
    equity: Decimal,
    tmn: Decimal,
    exposure_pct: Decimal,
    drawdown_pct: Decimal,
) -> None:
    conn.execute(
        "INSERT INTO equity_curve (ts, mode, run_id, equity_tmn, tmn, exposure_pct, drawdown_pct)"
        " VALUES (?,?,?,?,?,?,?)",
        (now_iso(), mode, run_id, _s(equity), _s(tmn), _s(exposure_pct), _s(drawdown_pct)),
    )


def insert_depth_snapshot(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    bids: list[list[str]],
    asks: list[list[str]],
) -> None:
    conn.execute(
        "INSERT INTO depth_snapshots (ts, symbol, best_bid, best_ask, bids, asks)"
        " VALUES (?,?,?,?,?,?)",
        (now_iso(), symbol, _s(best_bid), _s(best_ask), dumps(bids), dumps(asks)),
    )


def upsert_candles(conn: sqlite3.Connection, symbol: str, resolution: str, bars: list[Any]) -> None:
    # One transaction for the batch (autocommit would commit, and sync, per row).
    with conn:
        _upsert_candles(conn, symbol, resolution, bars)


def _upsert_candles(
    conn: sqlite3.Connection, symbol: str, resolution: str, bars: list[Any]
) -> None:
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT OR REPLACE INTO candles (symbol, resolution, ts, open, high, low, close, volume)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(symbol, resolution, b.t, _s(b.o), _s(b.h), _s(b.l), _s(b.c), _s(b.v)) for b in bars],
    )


def paper_stats(conn: sqlite3.Connection, run_id: str | None = None) -> list[sqlite3.Row]:
    """Per strategy/source: trade count, net PnL, gross profit/loss (profit factor)."""
    where, args = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    return conn.execute(
        "SELECT strategy, source, SUM(kind != 'conversion') AS n,"
        " SUM(CAST(net_tmn AS REAL)) AS net,"
        " SUM(CASE WHEN CAST(net_tmn AS REAL) > 0 THEN CAST(net_tmn AS REAL) ELSE 0 END) AS gp,"
        " SUM(CASE WHEN CAST(net_tmn AS REAL) < 0 THEN -CAST(net_tmn AS REAL) ELSE 0 END) AS gl"
        f" FROM paper_trades {where} GROUP BY strategy, source",
        args,
    ).fetchall()
