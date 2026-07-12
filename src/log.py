"""Structured logging setup (structlog). LOG_FORMAT=json for machine-readable output."""

from __future__ import annotations

import logging
import os

import structlog


def configure_logging() -> None:
    renderer: structlog.typing.Processor
    if os.getenv("LOG_FORMAT", "console") == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(module=module)
