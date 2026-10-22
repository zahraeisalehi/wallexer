"""Candle loading from /v1/udf/history, and local resampling.

Verified 2026-10-22: the endpoint serves only resolutions 1, 60 and D.
- `5` and `30` return `s: "error"` ("Invalid resolution!"); `15` silently returns 1-min bars.
  So 5- and 15-min bars are resampled here from 1-min bars.
- 1-min: at most 1 day per request (longer ranges return `no_data`).
- 60-min: a 31-day request returned only 29 days, so it is fetched in 7-day chunks.
Bars can be missing (gaps); indicators work on the bars present.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError

DAY_S = 86400
CHUNK_S = {"1": DAY_S, "60": 7 * DAY_S}  # max range per request, per native resolution


@dataclass(frozen=True)
class Bar:
    t: int  # open time, unix seconds
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


async def fetch_bars(
    client: WallexClient, symbol: str, from_ts: int, to_ts: int, resolution: str = "1"
) -> list[Bar]:
    """Native bars in [from_ts, to_ts], fetched in chunks, deduplicated and sorted."""
    chunk = CHUNK_S[resolution]
    out: dict[int, Bar] = {}
    start = from_ts
    while start < to_ts:
        end = min(start + chunk, to_ts)
        try:
            c = await client.udf_history(symbol, resolution, start, end)
        except (ApiError, TransportError):
            start = end
            continue
        if c.s == "ok":
            for i, t in enumerate(c.t):
                out[t] = Bar(t, c.o[i], c.h[i], c.l[i], c.c[i], c.v[i])
        start = end
    return [out[t] for t in sorted(out)]


def completed(bars: Sequence[Bar], minutes: int = 1, now: float | None = None) -> list[Bar]:
    """Drop the still-forming bar (its period has not ended yet)."""
    now = time.time() if now is None else now
    return [b for b in bars if b.t + minutes * 60 <= now]


def resample(bars: Sequence[Bar], minutes: int) -> list[Bar]:
    """Aggregate sorted 1-min bars into `minutes` buckets: o first, h max, l min, c last, v sum.

    A bucket is emitted with whatever 1-min bars it has (gaps are not filled). The caller drops
    the still-forming last bucket with `completed(..., minutes)`.
    """
    if minutes == 1:
        return list(bars)
    size = minutes * 60
    out: list[Bar] = []
    cur: list[Bar] = []
    cur_start = None
    for b in bars:
        start = b.t - b.t % size
        if start != cur_start and cur:
            out.append(_merge(cur_start, cur))  # type: ignore[arg-type]
            cur = []
        cur_start = start
        cur.append(b)
    if cur:
        out.append(_merge(cur_start, cur))  # type: ignore[arg-type]
    return out


def _merge(start: int, bs: list[Bar]) -> Bar:
    return Bar(
        start,
        bs[0].o,
        max(b.h for b in bs),
        min(b.l for b in bs),
        bs[-1].c,
        sum((b.v for b in bs), Decimal(0)),
    )
