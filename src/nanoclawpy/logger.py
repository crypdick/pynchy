"""Structured logging singleton.

Port of src/logger.ts — structlog replaces pino.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def _setup_logging() -> structlog.stdlib.BoundLogger:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Set stdlib logging level so structlog respects it
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)

    return structlog.get_logger()


logger = _setup_logging()


def _uncaught_exception_handler(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: object,
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)  # type: ignore[arg-type]
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.exit(1)


sys.excepthook = _uncaught_exception_handler
