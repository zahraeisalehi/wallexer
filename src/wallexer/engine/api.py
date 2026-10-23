"""Read-only view API, served next to /health (same port).

GET /api/status | /api/log?n=&after= | /api/reason[?after=] | /api/reason/{id}
    | /api/rslt/{backtest|shadowtest} | /api/equity?limit= | /api/sum?date=

Safety:
- There are no write handlers. Any method other than GET is answered 405 before routing.
- The DB is opened read-only (`mode=ro` + `PRAGMA query_only`), separate from the engine's own
  connection, so even a bug in a query cannot change state.
- Auth: header `X-View-Token` must equal env WALLEXER_VIEW_TOKEN (constant-time compare). With the
  variable unset the API does not exist (404): it is never open by accident.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from wallexer.cli import views

TOKEN_ENV = "WALLEXER_VIEW_TOKEN"
MAX_ROWS = 2000


class ReadOnlyApi:
    def __init__(
        self,
        db_path: Path,
        status: Callable[[], dict[str, Any]],
        start_equity: float,
        capital: float,
    ) -> None:
        self.db_path = db_path
        self.status = status
        self.start_equity = start_equity
        self.capital = capital

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        return conn

    async def handle(
        self, method: str, target: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            return 404, {"error": "not found"}
        if method != "GET":
            return 405, {"error": "read-only API: GET only"}
        given = headers.get("x-view-token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            return 401, {"error": "missing or wrong X-View-Token"}
        url = urlsplit(target)
        parts = [p for p in url.path.split("/") if p][1:]  # drop "api"
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if parts == ["status"]:
                # engine state lives on the event loop: read it here, not in a thread
                return 200, self.status()
            return await asyncio.to_thread(self._db_route, parts, q)
        except ValueError:
            return 400, {"error": "bad parameter"}
        except sqlite3.Error as e:
            return 503, {"error": f"database unavailable: {str(e)[:80]}"}

    def _db_route(self, parts: list[str], q: dict[str, str]) -> tuple[int, dict[str, Any]]:
        conn = self._connect()
        try:
            if parts == ["log"]:
                n = int(q["n"]) if q.get("n") else None
                after = int(q["after"]) if q.get("after") else None
                limit = min(int(q.get("limit", 500)), MAX_ROWS)
                return 200, views.log_data(conn, n, after, limit)
            if parts == ["reason"]:
                if q.get("after"):  # live view: only decisions newer than `after`
                    return 200, {"decisions": views.decisions_after(conn, int(q["after"]))}
                return 200, views.reason_overview_data(conn)
            if parts == ["sum"]:
                from wallexer.cli import daily

                day = daily.parse_day(q.get("date"))
                try:
                    cached = conn.execute(
                        "SELECT explanation FROM daily_summaries WHERE date=? AND origin='LOCAL'",
                        (day.isoformat(),),
                    ).fetchone()
                except sqlite3.OperationalError:  # DB created before the cache table existed
                    cached = None
                return 200, {
                    "facts": daily.daily_facts(conn, day),
                    "explanation": json.loads(cached[0]) if cached and cached[0] else None,
                }
            if len(parts) == 2 and parts[0] == "reason":
                d = views.reason_data(conn, int(parts[1]))
                return (200, d) if d else (404, {"error": "no such decision"})
            if parts == ["rslt", "backtest"]:
                return 200, {"result": views.rslt_backtest_data(conn, self.capital)}
            if parts == ["rslt", "shadowtest"]:
                return 200, {
                    "result": views.rslt_shadow_data(conn, self.start_equity, self.capital)
                }
            if parts == ["equity"]:
                return 200, views.equity_data(conn, min(int(q.get("limit", 500)), MAX_ROWS))
            return 404, {"error": "not found"}
        finally:
            conn.close()
