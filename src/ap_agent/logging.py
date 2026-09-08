"""structlog configuration.

Every log line is a structured event. In an AP pipeline the log is a debugging
aid, not the record of truth - the record of truth is the ``audit_events``
table. Nothing here may be relied on for compliance.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from ap_agent.config import get_settings

_configured = False


def configure_logging(*, force: bool = False) -> None:
    """Configure structlog once per process.

    Args:
        force: Reconfigure even if this has already run. Tests use this.
    """
    global _configured  # noqa: PLW0603 - process-wide, idempotent guard
    if _configured and not force:
        return

    settings = get_settings()
    logging.basicConfig(format="%(message)s", level=settings.log_level)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring structlog on first use."""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
