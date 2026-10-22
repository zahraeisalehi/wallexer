"""structlog setup. The engine logs JSON to stdout; CLI commands keep stderr quiet (warnings only)."""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: int = logging.WARNING, json: bool = False) -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )
