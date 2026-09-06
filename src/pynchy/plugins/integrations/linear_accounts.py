"""Resolve one named Linear tool declaration to one provider account."""

from __future__ import annotations

import os
from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass

from pynchy.integration_contracts import (
    LinearAccountConfig,
    is_linear_account_config,
)


@dataclass(frozen=True)
class LinearAccount:
    """A Linear credential and its data-flow declarations."""

    name: str
    config: LinearAccountConfig

    @property
    def api_key(self) -> str | None:
        """Read this account's API key without falling back to another account."""
        return os.environ.get(self.config.api_key_env)

    @property
    def team_key(self) -> str | None:
        """Read the optional team selector paired with this account."""
        return os.environ.get(self.config.team_key_env)


@dataclass(frozen=True)
class LinearAccountRuntime:
    """Configured Linear tools and workspace selections from application composition."""

    tools: Mapping[str, object]
    workspace_tool_names: Callable[[str], tuple[str, ...] | None]


_state: LinearAccountRuntime | None = None


def configure_linear_account_runtime(runtime: LinearAccountRuntime) -> None:
    """Inject the configured Linear accounts and workspace selectors."""
    global _state  # noqa: PLW0603 - one host process owns this configured runtime.
    _state = runtime


def _configured_runtime() -> LinearAccountRuntime:
    if _state is None:
        raise RuntimeError("Linear account runtime has not been configured")
    return _state


def configured_linear_accounts(
    tools: Mapping[str, object] | None = None,
) -> tuple[LinearAccount, ...]:
    """Return every named Linear tool as an independently trusted account."""
    current_tools = _configured_runtime().tools if tools is None else tools
    return tuple(
        LinearAccount(name, tool)
        for name, tool in sorted(current_tools.items())
        if is_linear_account_config(tool)
    )


def linear_account(name: str, tools: Mapping[str, object] | None = None) -> LinearAccount:
    """Resolve an exact configured Linear account name."""
    current_tools = _configured_runtime().tools if tools is None else tools
    tool = current_tools.get(name)
    if not is_linear_account_config(tool):
        raise TypeError(f"Linear account tool is not configured: {name}")
    return LinearAccount(name, tool)


def linear_account_for_workspace(
    workspace: str,
    *,
    tools: Mapping[str, object] | None = None,
    workspace_tool_names: Callable[[str], tuple[str, ...] | None] | None = None,
) -> LinearAccount | None:
    """Resolve the single Linear account selected by a workspace."""
    runtime = _configured_runtime()
    current_tools = runtime.tools if tools is None else tools
    resolve_tools = (
        runtime.workspace_tool_names if workspace_tool_names is None else workspace_tool_names
    )
    selected_tools = resolve_tools(workspace)
    if selected_tools is None:
        return None
    accounts = tuple(
        LinearAccount(name, tool)
        for name in selected_tools
        if is_linear_account_config(tool := current_tools.get(name))
    )
    if len(accounts) > 1:
        names = ", ".join(account.name for account in accounts)
        raise ValueError(
            f"Workspace '{workspace}' must select exactly one Linear account; found: {names}"
        )
    return accounts[0] if accounts else None
