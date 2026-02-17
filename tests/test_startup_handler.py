"""Tests for pynchy.startup_handler — startup helpers and plugin credential validation."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pynchy.startup_handler import auto_rollback, validate_plugin_credentials

# ---------------------------------------------------------------------------
# validate_plugin_credentials
# ---------------------------------------------------------------------------


class TestValidatePluginCredentials:
    """Tests for checking plugin environment variable requirements."""

    def test_returns_empty_when_no_requires_credentials(self):
        """Plugins without requires_credentials() need no credentials."""

        class NoCredsPlugin:
            pass

        assert validate_plugin_credentials(NoCredsPlugin()) == []

    def test_returns_empty_when_all_present(self, monkeypatch):
        """All required credentials are in the environment."""

        class Plugin:
            def requires_credentials(self):
                return ["MY_API_KEY", "MY_SECRET"]

        monkeypatch.setenv("MY_API_KEY", "key-123")
        monkeypatch.setenv("MY_SECRET", "secret-456")
        assert validate_plugin_credentials(Plugin()) == []

    def test_returns_missing_credentials(self, monkeypatch):
        """Missing credentials are returned in the list."""

        class Plugin:
            def requires_credentials(self):
                return ["PRESENT_KEY", "MISSING_KEY"]

        monkeypatch.setenv("PRESENT_KEY", "value")
        monkeypatch.delenv("MISSING_KEY", raising=False)
        result = validate_plugin_credentials(Plugin())
        assert result == ["MISSING_KEY"]

    def test_returns_all_missing_when_none_present(self, monkeypatch):
        """All credentials missing when none are in the environment."""

        class Plugin:
            def requires_credentials(self):
                return ["KEY_A", "KEY_B"]

        monkeypatch.delenv("KEY_A", raising=False)
        monkeypatch.delenv("KEY_B", raising=False)
        result = validate_plugin_credentials(Plugin())
        assert set(result) == {"KEY_A", "KEY_B"}

    def test_empty_requires_list(self):
        """Plugin requires no credentials (empty list)."""

        class Plugin:
            def requires_credentials(self):
                return []

        assert validate_plugin_credentials(Plugin()) == []


# ---------------------------------------------------------------------------
# auto_rollback
# ---------------------------------------------------------------------------


class TestAutoRollback:
    """Tests for auto_rollback — rolls back to previous commit on startup failure."""

    @pytest.mark.asyncio
    async def test_skips_when_file_unreadable(self, tmp_path):
        """Should return early when continuation file can't be read."""
        bad_path = tmp_path / "continuation.json"
        bad_path.write_text("not valid json")

        await auto_rollback(bad_path, RuntimeError("startup failed"))
        # Should not raise — just logs and returns

    @pytest.mark.asyncio
    async def test_skips_when_no_previous_sha(self, tmp_path):
        """Should return early when previous_commit_sha is empty."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(json.dumps({"previous_commit_sha": ""}))

        await auto_rollback(cont_path, RuntimeError("startup failed"))
        # Should not raise — just logs and returns

    @pytest.mark.asyncio
    async def test_performs_rollback_and_rewrites_continuation(self, tmp_path):
        """Should git reset to previous SHA and rewrite continuation."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(
            json.dumps(
                {
                    "previous_commit_sha": "abc123def",
                    "resume_prompt": "Deploy complete.",
                }
            )
        )

        class FakeResult:
            returncode = 0
            stderr = ""

        with (
            patch("pynchy.startup_handler.run_git", return_value=FakeResult()) as mock_git,
            pytest.raises(SystemExit) as exc_info,
        ):
            await auto_rollback(cont_path, RuntimeError("startup failed"))

        mock_git.assert_called_once_with("reset", "--hard", "abc123def")
        assert exc_info.value.code == 1

        # Continuation should be rewritten with rollback info
        updated = json.loads(cont_path.read_text())
        assert "ROLLBACK" in updated["resume_prompt"]
        assert updated["previous_commit_sha"] == ""  # prevents loop

    @pytest.mark.asyncio
    async def test_returns_when_git_reset_fails(self, tmp_path):
        """Should return (not exit) when git reset fails."""
        cont_path = tmp_path / "continuation.json"
        cont_path.write_text(json.dumps({"previous_commit_sha": "abc123def"}))

        class FailResult:
            returncode = 1
            stderr = "fatal: not a git repo"

        with patch("pynchy.startup_handler.run_git", return_value=FailResult()):
            await auto_rollback(cont_path, RuntimeError("startup failed"))
        # Should not raise — git reset failure is logged and returned

    @pytest.mark.asyncio
    async def test_skips_when_file_does_not_exist(self, tmp_path):
        """Should return early when continuation file doesn't exist."""
        missing_path = tmp_path / "no_such_file.json"

        await auto_rollback(missing_path, RuntimeError("startup failed"))
        # Should not raise
