"""Tests for the host log streams."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch

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


def test_configuring_a_new_error_log_replaces_the_existing_handler(tmp_path: Path):
    first_log = (tmp_path / "first.log").resolve()
    second_log = (tmp_path / "second.log").resolve()
    try:
        configure_error_log(first_log)
        configure_error_log(second_log)

        handlers = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, logging.FileHandler)
        ]
        assert not any(Path(handler.baseFilename) == first_log for handler in handlers)
        assert any(Path(handler.baseFilename) == second_log for handler in handlers)
    finally:
        _remove_error_log_handler(first_log)
        _remove_error_log_handler(second_log)


def test_keyboard_interrupt_uses_the_original_exception_hook(monkeypatch):
    original_hook = Mock()
    monkeypatch.setattr(sys, "__excepthook__", original_hook)
    exception = KeyboardInterrupt()

    sys.excepthook(KeyboardInterrupt, exception, None)

    original_hook.assert_called_once_with(KeyboardInterrupt, exception, None)


def test_uncaught_exception_is_logged_and_exits(monkeypatch):
    critical = Mock()
    exit_process = Mock()
    monkeypatch.setattr(sys, "exit", exit_process)
    exception = RuntimeError("uncaught")

    with patch("pynchy.logger.logger.critical", critical):
        sys.excepthook(RuntimeError, exception, None)

    critical.assert_called_once_with("Uncaught exception", exc_info=(RuntimeError, exception, None))
    exit_process.assert_called_once_with(1)


def test_error_log_does_not_render_traceback_locals(tmp_path: Path):
    error_log = (tmp_path / "pynchy.error.log").resolve()
    try:
        configure_error_log(error_log)
        credential_that_must_not_leak = "lin_api_logger_" + "traceback_must_not_leak"
        try:
            raise RuntimeError("provider request failed")
        except RuntimeError:
            logger.exception("Linear request failed")

        contents = error_log.read_text(encoding="utf-8")
    finally:
        _remove_error_log_handler(error_log)

    assert "provider request failed" in contents
    assert credential_that_must_not_leak not in contents
