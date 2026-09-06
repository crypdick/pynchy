"""Structured logging singleton.

Intentionally reads os.environ directly — logger must initialize before
pydantic Settings to avoid circular imports and ensure early logging.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import structlog

_ERROR_LOG_HANDLER_NAME = "pynchy-error-log"


def _setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Configure stdlib root logger first so structlog's filter_by_level works
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            # Exception frames routinely contain credentials and provider
            # payloads. Preserve the traceback while never rendering locals.
            structlog.dev.ConsoleRenderer(
                colors=sys.stderr.isatty(),
                exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
            ),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


_setup_logging()
logger = structlog.stdlib.get_logger()


def configure_error_log(log_path: Path) -> None:
    """Write only ERROR and CRITICAL records to ``log_path``.

    The process standard-error stream retains every application record for
    chronological troubleshooting. This handler provides the narrow view used
    for error review without changing the general log contents.
    """
    root_logger = logging.getLogger()
    resolved_path = log_path.resolve()
    existing_handler = next(
        (handler for handler in root_logger.handlers if handler.name == _ERROR_LOG_HANDLER_NAME),
        None,
    )
    if existing_handler is not None:
        if (
            isinstance(existing_handler, logging.FileHandler)
            and Path(existing_handler.baseFilename) == resolved_path
        ):
            return
        root_logger.removeHandler(existing_handler)
        existing_handler.close()

    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    error_handler = logging.FileHandler(resolved_path, encoding="utf-8")
    error_handler.name = _ERROR_LOG_HANDLER_NAME
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(error_handler)


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
