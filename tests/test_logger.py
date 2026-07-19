"""Tests for the host log streams."""

from __future__ import annotations

import logging
from pathlib import Path

from pynchy.logger import configure_error_log, logger


def _remove_error_log_handler(log_path: Path) -> None:
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path:
            root_logger.removeHandler(handler)
            handler.close()


def test_error_log_excludes_non_errors(tmp_path: Path):
    error_log = (tmp_path / "logs" / "pynchy.error.log").resolve()
    try:
        configure_error_log(error_log)

        logger.warning("This warning belongs only in the general log")
        logger.error("This error belongs in the error log")

        contents = error_log.read_text(encoding="utf-8")
    finally:
        _remove_error_log_handler(error_log)

    assert "This warning belongs only in the general log" not in contents
    assert "This error belongs in the error log" in contents


def test_configuring_the_same_error_log_is_idempotent(tmp_path: Path):
    error_log = (tmp_path / "pynchy.error.log").resolve()
    try:
        configure_error_log(error_log)
        first_handler = next(
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == error_log
        )

        configure_error_log(error_log)
        handlers = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == error_log
        ]
    finally:
        _remove_error_log_handler(error_log)

    assert handlers == [first_handler]
