"""Client-side rate limiters.

Two independent limiters, so market-data polling can never starve order traffic and vice versa:
- MarketDataLimiter follows the x-ratelimit-* headers the exchange returns.
- OrderLimiter enforces the exchange's order rule (max 20 create+cancel per 10 s) with our own
  stricter cap, queueing requests (FIFO) instead of dropping them.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

import structlog

log = structlog.get_logger(__name__)

EventSink = Callable[[str, dict[str, Any]], None]


def _noop_sink(kind: str, payload: dict[str, Any]) -> None:
    return None


class MarketDataLimiter:
    """Header-driven limiter: when remaining drops under a fraction of the limit, wait for reset.

    The window length is not documented, so it comes from config (measured by `doctor`).
    A window is assumed to start when `remaining` is seen to go *up*.
    """

    def __init__(
        self,
        window_s: float,
        backoff_below_frac: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_s = window_s
        self.frac = backoff_below_frac
        self.clock = clock
        self.limit: int | None = None
        self.remaining: int | None = None
        self.window_start: float = clock()
        self._lock = asyncio.Lock()

    def update(self, headers: Mapping[str, str]) -> None:
        try:
            limit = int(headers["x-ratelimit-limit"])
            remaining = int(headers["x-ratelimit-remaining"])
        except (KeyError, ValueError):
            return
        if self.remaining is not None and remaining > self.remaining:
            self.window_start = self.clock()
        self.limit, self.remaining = limit, remaining

    def wait_needed(self) -> float:
        if self.limit is None or self.remaining is None:
            return 0.0
        if self.remaining >= self.limit * self.frac:
            return 0.0
        return max(0.0, self.window_start + self.window_s - self.clock())

    async def acquire(self) -> None:
        async with self._lock:
            delay = self.wait_needed()
            if delay > 0:
                log.warning("ratelimit.market_backoff", remaining=self.remaining, sleep_s=delay)
                await asyncio.sleep(delay)
                # Assume a fresh window after waiting it out; the next response corrects this.
                self.remaining = None
                self.window_start = self.clock()


class OrderLimiter:
    """Sliding window over order requests (create AND cancel), FIFO-queued.

    asyncio.Lock wakes waiters in FIFO order, so queued order requests keep their sequence.
    A backlog (too many waiters, or a wait longer than one window) is reported as a risk event.
    """

    def __init__(
        self,
        max_requests: int,
        window_s: float,
        backlog_warn: int,
        on_event: EventSink = _noop_sink,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self.backlog_warn = backlog_warn
        self.on_event = on_event
        self.clock = clock
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.waiting = 0
        self._last_backlog: float | None = None
        self.backlog_suppressed = 0

    def _prune(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.window_s:
            self._sent.popleft()

    def _backlog(self, reason: str, **extra: Any) -> None:
        # At most one event per window: a burst would otherwise log one row per queued request.
        now = self.clock()
        if self._last_backlog is not None and now - self._last_backlog < self.window_s:
            self.backlog_suppressed += 1
            return
        self._last_backlog = now
        payload = {
            "reason": reason,
            "waiting": self.waiting,
            "suppressed_since_last": self.backlog_suppressed,
            **extra,
        }
        self.backlog_suppressed = 0
        log.warning("risk.order_queue_backlog", **payload)
        self.on_event("risk.order_queue_backlog", payload)

    async def acquire(self, what: str) -> None:
        """Block until one order request (`what` = 'create' | 'cancel') may be sent."""
        enqueued = self.clock()
        self.waiting += 1
        if self.waiting >= self.backlog_warn:
            self._backlog("queue_depth", what=what)
        try:
            async with self._lock:
                while True:
                    now = self.clock()
                    self._prune(now)
                    if len(self._sent) < self.max_requests:
                        break
                    await asyncio.sleep(self._sent[0] + self.window_s - now + 0.001)
                waited = self.clock() - enqueued
                if waited > self.window_s:
                    self._backlog("long_wait", what=what, waited_s=round(waited, 2))
                self._sent.append(self.clock())
        finally:
            self.waiting -= 1
