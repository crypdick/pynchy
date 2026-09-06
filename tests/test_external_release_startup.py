"""Immutable image startup failures preserve release-controller ownership."""

import json
from unittest.mock import patch

import pytest

from pynchy.host.orchestrator.startup_handler import auto_rollback


@pytest.mark.asyncio
async def test_external_release_preserves_checkout_and_continuation(self, tmp_path, monkeypatch):
    monkeypatch.setenv("PYNCHY_RELEASE_SHA", "a" * 40)
    cont_path = tmp_path / "continuation.json"
    original = json.dumps({"previous_commit_sha": "prev-sha-1", "commit_sha": "a" * 40})
    cont_path.write_text(original)
    with (
        patch("pynchy.host.orchestrator.startup_handler.run_git") as git,
        patch("pynchy.host.orchestrator.startup_handler.terminate_failed_startup") as terminate,
    ):
        await auto_rollback(cont_path, RuntimeError("startup failed"))
    git.assert_not_called()
    terminate.assert_not_called()
    assert cont_path.read_text() == original
    assert not (tmp_path / "boot_warnings.json").exists()
