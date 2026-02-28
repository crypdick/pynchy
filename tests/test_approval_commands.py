"""Tests for approval command matchers."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import make_settings


@pytest.fixture(autouse=True)
def _mock_settings():
    """Provide default settings so _strip_trigger works."""
    with patch("pynchy.host.orchestrator.messaging.commands.get_settings", return_value=make_settings()):
        yield


class TestIsApprovalCommand:
    def test_approve_with_hex_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        result = is_approval_command("approve a7f3b2c1")
        assert result == ("approve", "a7f3b2c1")

    def test_deny_with_hex_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        result = is_approval_command("deny a7f3b2c1")
        assert result == ("deny", "a7f3b2c1")

    def test_case_insensitive(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        result = is_approval_command("Approve A7F3B2C1")
        assert result == ("approve", "a7f3b2c1")

    def test_rejects_non_alnum_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        assert is_approval_command("approve not-hex!") is None

    def test_rejects_wrong_verb(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        assert is_approval_command("accept abc12345") is None

    def test_rejects_too_many_words(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        assert is_approval_command("approve abc12345 extra") is None

    def test_rejects_bare_approve(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        assert is_approval_command("approve") is None

    def test_accepts_2_char_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        result = is_approval_command("approve a7")
        assert result == ("approve", "a7")

    def test_rejects_1_char_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        assert is_approval_command("approve a") is None

    def test_accepts_non_hex_alphanumeric(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        # 'z' and 'q' are not hex digits but should be accepted
        result = is_approval_command("approve zq")
        assert result == ("approve", "zq")

    def test_accepts_full_request_id(self):
        from pynchy.host.orchestrator.messaging.commands import is_approval_command

        full_id = "a7f3b2c1d4e5f6a7b8c9d0e1f2a3b4c5"
        result = is_approval_command(f"approve {full_id}")
        assert result == ("approve", full_id)


class TestIsPendingQuery:
    def test_bare_pending(self):
        from pynchy.host.orchestrator.messaging.commands import is_pending_query

        assert is_pending_query("pending") is True

    def test_case_insensitive(self):
        from pynchy.host.orchestrator.messaging.commands import is_pending_query

        assert is_pending_query("Pending") is True

    def test_rejects_other_text(self):
        from pynchy.host.orchestrator.messaging.commands import is_pending_query

        assert is_pending_query("show pending items") is False

    def test_rejects_empty(self):
        from pynchy.host.orchestrator.messaging.commands import is_pending_query

        assert is_pending_query("") is False
