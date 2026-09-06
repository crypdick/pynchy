"""The prose review reports findings without blocking configured hooks."""

from __future__ import annotations

import subprocess  # noqa: S404 - executes the repository checker with fixed arguments.
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(("arguments", "expected_status"), [([], 1), (["--advisory"], 0)])
def test_comment_review_exit_status(tmp_path, arguments, expected_status):
    source = tmp_path / "example.py"
    source.write_text("# Retain the original request for diagnostics.\n")
    checker = Path(__file__).resolve().parents[1] / "scripts/prek_hooks/check_timeless_comments.py"
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script.
        [sys.executable, str(checker), *arguments, str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_status
    assert "Temporal keyword" in result.stdout
    assert "original" in result.stdout
