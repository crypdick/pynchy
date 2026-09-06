"""Linear account resolution boundary contracts."""

from __future__ import annotations

import pytest

from pynchy.plugins.integrations.linear_accounts import (
    configured_linear_accounts,
    linear_account,
)


def test_configured_linear_accounts_requires_composed_runtime(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_accounts._state", None)

    with pytest.raises(RuntimeError, match="Linear account runtime has not been configured"):
        configured_linear_accounts()


def test_linear_account_rejects_unknown_tool_name() -> None:
    with pytest.raises(TypeError, match="Linear account tool is not configured: missing"):
        linear_account("missing", tools={})
