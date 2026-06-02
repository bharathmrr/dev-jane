"""Structured logging using structlog.

Emits JSON in production (LOG_JSON=true) and a colorized console renderer in
dev. A contextvar carries a request/correlation id so every log line within a
request or Celery task can be traced.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.core.config import settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(_, __, event_dict: dict) -> dict:
    rid = request_id_ctx.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging() -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _add_request_id,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON
        else structlog.dev.ConsoleRenderer()
    )

    # Set up standard logging handlers to write to both stdout and a backend.log file in the workspace root.
    file_handler = logging.FileHandler("backend.log", encoding="utf-8")
    stdout_handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(
        format="%(message)s",
        level=settings.LOG_LEVEL,
        handlers=[file_handler, stdout_handler],
    )

    structlog.configure(
        processors=shared + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.LOG_LEVEL)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
