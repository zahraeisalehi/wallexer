"""End-of-event schedule: no new entries from T-90 min, force flatten from T-30 min."""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    unknown = "unknown"  # EVENT_END_UTC not set: entries allowed, no auto-flatten
    normal = "normal"
    no_entries = "no_entries"
    flatten = "flatten"
    ended = "ended"


def phase(
    now: float, end: float | None, no_entry_before_s: float, flatten_before_s: float
) -> Phase:
    if end is None:
        return Phase.unknown
    left = end - now
    if left <= 0:
        return Phase.ended
    if left <= flatten_before_s:
        return Phase.flatten
    if left <= no_entry_before_s:
        return Phase.no_entries
    return Phase.normal


def entries_allowed(p: Phase) -> bool:
    return p in (Phase.normal, Phase.unknown)
