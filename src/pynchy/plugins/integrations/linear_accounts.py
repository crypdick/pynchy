"""Resolve one named Linear tool declaration to one provider account."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pynchy.config import Settings, get_settings
from pynchy.plugins.integrations.linear_config import LinearTool


@dataclass(frozen=True)
class LinearAccount:
    """A Linear credential and its data-flow declarations."""

    name: str
    config: LinearTool

    @property
    def api_key(self) -> str | None:
        """Read this account's API key without falling back to another account."""
        return os.environ.get(self.config.api_key_env)

    @property
    def team_key(self) -> str | None:
        """Read the optional team selector paired with this account."""
        return os.environ.get(self.config.team_key_env)


def configured_linear_accounts(settings: Settings | None = None) -> tuple[LinearAccount, ...]:
    """Return every named Linear tool as an independently trusted account."""
    current = settings or get_settings()
    return tuple(
        LinearAccount(name, tool)
        for name, tool in sorted(current.tools.items())
        if isinstance(tool, LinearTool)
    )


def linear_account(name: str, settings: Settings | None = None) -> LinearAccount:
    """Resolve an exact configured Linear account name."""
    current = settings or get_settings()
    tool = current.tools.get(name)
    if not isinstance(tool, LinearTool):
        raise TypeError(f"Linear account tool is not configured: {name}")
    return LinearAccount(name, tool)


def linear_account_for_workspace(
    workspace: str,
    settings: Settings | None = None,
) -> LinearAccount | None:
    """Resolve the single Linear account selected by a workspace."""
    current = settings or get_settings()
    resolved = current.resolved_workspace_config(workspace)
    if resolved is None:
        return None
    accounts = tuple(
        LinearAccount(name, tool)
        for name in resolved.tools
        if isinstance((tool := current.tools.get(name)), LinearTool)
    )
    if len(accounts) > 1:
        names = ", ".join(account.name for account in accounts)
        raise ValueError(
            f"Workspace '{workspace}' must select exactly one Linear account; found: {names}"
        )
    return accounts[0] if accounts else None
