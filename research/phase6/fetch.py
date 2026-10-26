"""Phase 6 data: everything goes to data/research/phase6.db (the engine's DB is only read).

- 1-min bars of the 7 markets: copied from the engine DB, topped up to now from Wallex.
- Wallex 60-min (7-day chunks) and daily bars, as far back as the API serves them.
- Nobitex public UDF 1-min bars for BTCIRT / USDTIRT over the same window (IRT / 10 = TMN).
- The account fee table (read-only private call), recorded for the cost model.
Run: uv run python research/phase6/fetch.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx

from wallexer.config import load_settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "research" / "phase6.db"
MARKETS = ["BTCTMN", "ETHTMN", "XAUTTMN", "USDTTMN", "BTCUSDT", "ETHUSDT", "XAUTUSDT"]
DAY = 86400


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS bars (source TEXT, symbol TEXT, res TEXT, ts INTEGER,"
        " o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (source, symbol, res, ts))"
    )
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return c


def put(c: sqlite3.Connection, source: str, sym: str, res: str, rows: list[tuple]) -> int:
    c.executemany(
        "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
        [(source, sym, res, *r) for r in rows],
    )
    c.commit()
    return len(rows)


async def udf(client: WallexClient, sym: str, res: str, a: int, b: int) -> list[tuple]:
    try:
        x = await client.udf_history(sym, res, a, b)
    except (ApiError, TransportError):
        return []
    if x.s != "ok":
        return []
    return [
        (t, float(x.o[i]), float(x.h[i]), float(x.l[i]), float(x.c[i]), float(x.v[i]))
        for i, t in enumerate(x.t)
    ]


async def wallex(c: sqlite3.Connection) -> None:
    s = load_settings()
    client = WallexClient(s)
    now = int(time.time())
    try:
        # fee table (read-only)
        try:
            ft = await client.fees()
            fees = {
                m: [str(ft.get(m).maker), str(ft.get(m).taker), ft.get(m).source] for m in MARKETS
            }
        except Exception as e:  # recorded, the documented table is the fallback
            fees = {"error": f"{type(e).__name__}"}
        c.execute("INSERT OR REPLACE INTO meta VALUES ('fees', ?)", (json.dumps(fees),))
        # 1-min: engine DB copy + top-up
        eng = sqlite3.connect(f"file:{ROOT / 'data' / 'wallexer.db'}?mode=ro", uri=True)
        for m in MARKETS:
            rows = eng.execute(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? AND resolution='1'",
                (m,),
            ).fetchall()
            put(c, "wallex", m, "1", [(t, *map(float, r)) for t, *r in rows])
            last = max((r[0] for r in rows), default=now - 30 * DAY)
            top = []
            a = last
            while a < now:
                top += await udf(client, m, "1", a, min(a + DAY, now))
                a += DAY
            print(m, "1m", len(rows), "+", put(c, "wallex", m, "1", top))
        eng.close()
        # 60-min, 7-day chunks backwards until 3 empty chunks in a row
        for m in MARKETS:
            got, empty, b = 0, 0, now
            while empty < 3 and b > now - 800 * DAY:
                rows = await udf(client, m, "60", b - 7 * DAY, b)
                empty = 0 if rows else empty + 1
                got += put(c, "wallex", m, "60", rows)
                b -= 7 * DAY
            print(m, "60m", got)
        # daily, 1-year chunks backwards
        for m in MARKETS:
            got, empty, b = 0, 0, now
            while empty < 2 and b > now - 2000 * DAY:
                rows = await udf(client, m, "D", b - 365 * DAY, b)
                empty = 0 if rows else empty + 1
                got += put(c, "wallex", m, "D", rows)
                b -= 365 * DAY
            print(m, "D", got)
    finally:
        await client.aclose()


def nobitex(c: sqlite3.Connection) -> None:
    lo = c.execute("SELECT MIN(ts) FROM bars WHERE source='wallex' AND res='1'").fetchone()[0]
    now = int(time.time())
    ok = True
    with httpx.Client(timeout=20, headers={"User-Agent": "wallexer-research/0.1"}) as h:
        for sym in ("BTCIRT", "USDTIRT"):
            got, a = 0, lo
            while a < now:
                b = min(a + DAY, now)
                try:
                    r = h.get(
                        "https://api.nobitex.ir/market/udf/history",
                        params={"symbol": sym, "resolution": "1", "from": a, "to": b},
                    )
                    j = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    print("nobitex", sym, "error", type(e).__name__)
                    ok = False
                    break
                if j.get("s") == "ok":
                    rows = [
                        (
                            t,
                            j["o"][i] / 10,
                            j["h"][i] / 10,
                            j["l"][i] / 10,
                            j["c"][i] / 10,
                            j["v"][i],
                        )
                        for i, t in enumerate(j["t"])
                    ]
                    got += put(c, "nobitex", sym, "1", rows)
                a = b
                time.sleep(0.25)  # polite: public endpoint
            print("nobitex", sym, got)
    c.execute("INSERT OR REPLACE INTO meta VALUES ('nobitex_ok', ?)", (json.dumps(ok),))
    c.commit()


if __name__ == "__main__":
    conn = db()
    asyncio.run(wallex(conn))
    nobitex(conn)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('fetched_at', ?)", (str(int(time.time())),))
    conn.commit()
