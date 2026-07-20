"""Tests for named Linear account resolution."""

from __future__ import annotations

from unittest.mock import ANY, patch

import pytest
from conftest import make_settings

from pynchy.config.models import LinearTool, ProfileConfig, WorkspaceConfig
from pynchy.plugins.integrations.linear_accounts import (
    linear_account,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_provider import linear_client


def test_account_resolves_its_own_credentials_and_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_SYNAPSE_API_KEY", "lin_synapse")
    monkeypatch.setenv("LINEAR_SYNAPSE_TEAM_KEY", "SYN")
    settings = make_settings(
        tools={
            "linear_synapse": LinearTool(
                type="linear",
                api_key_env="LINEAR_SYNAPSE_API_KEY",  # pragma: allowlist secret
                team_key_env="LINEAR_SYNAPSE_TEAM_KEY",
                public_source=False,
                secret_data=True,
                public_sink=False,
                dangerous_writes=False,
            )
        }
    )

    account = linear_account("linear_synapse", settings)

    assert account.api_key == "lin_synapse"  # pragma: allowlist secret
    assert account.team_key == "SYN"
    assert account.config.public_source is False
    assert account.config.secret_data is True
    assert account.config.public_sink is False


def test_workspace_must_select_at_most_one_linear_account() -> None:
    settings = make_settings(
        profiles={"both": ProfileConfig(tools=["linear_public", "linear_synapse"])},
        workspaces={"project": WorkspaceConfig(profiles=["both"])},
        tools={
            "linear_public": LinearTool(type="linear"),
            "linear_synapse": LinearTool(type="linear"),
        },
    )

    with pytest.raises(ValueError, match="exactly one Linear account"):
        linear_account_for_workspace("project", settings)


async def test_host_client_uses_the_workspace_accounts_exact_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "wrong-global-key")
    monkeypatch.setenv("LINEAR_SYNAPSE_API_KEY", "lin_synapse")
    monkeypatch.setenv("LINEAR_SYNAPSE_TEAM_KEY", "SYN")
    settings = make_settings(
        profiles={"synapse": ProfileConfig(tools=["linear_synapse"])},
        workspaces={"project": WorkspaceConfig(profiles=["synapse"])},
        tools={
            "linear_synapse": LinearTool(
                type="linear",
                api_key_env="LINEAR_SYNAPSE_API_KEY",  # pragma: allowlist secret
                team_key_env="LINEAR_SYNAPSE_TEAM_KEY",
            )
        },
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_accounts.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_work_item_provider.LinearClient",
            side_effect=LinearClient,
        ) as client_class,
    ):
        async with linear_client(workspace="project") as client:
            assert client.team_key == "SYN"

    client_class.assert_called_once_with(
        api_key="lin_synapse",  # pragma: allowlist secret
        session=ANY,
        team_key="SYN",
    )
