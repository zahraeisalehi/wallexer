"""1-minute candle loading from /v1/udf/history.

A single request for 3 days of 1-min bars returns `no_data`; 1-day chunks work (1440 bars), so
history is fetched day by day. Bars can be missing (gaps); indicators work on the bars present.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError

DAY_S = 86400


@dataclass(frozen=True)
class Bar:
    t: int  # open time, unix seconds
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


async def fetch_bars(client: WallexClient, symbol: str, from_ts: int, to_ts: int) -> list[Bar]:
    """Bars in [from_ts, to_ts], fetched in <= 1-day chunks, deduplicated and sorted."""
    out: dict[int, Bar] = {}
    start = from_ts
    while start < to_ts:
        end = min(start + DAY_S, to_ts)
        try:
            c = await client.udf_history(symbol, "1", start, end)
        except (ApiError, TransportError):
            start = end
            continue
        if c.s == "ok":
            for i, t in enumerate(c.t):
                out[t] = Bar(t, c.o[i], c.h[i], c.l[i], c.c[i], c.v[i])
        start = end
    return [out[t] for t in sorted(out)]


def completed(bars: list[Bar], now: float | None = None) -> list[Bar]:
    """Drop the still-forming bar (open time within the last 60 s)."""
    now = time.time() if now is None else now
    return [b for b in bars if b.t + 60 <= now]
