"""Build the explicitly authorized environment passed to agent processes.

LLM provider keys stay behind the gateway. Workspace tool variables enter an
agent process only when its selected tool explicitly authorizes that exposure.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - credential discovery uses fixed no-shell gh/git argv.
from collections.abc import (
    Callable,
)
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Host-side discovery helpers
# ---------------------------------------------------------------------------


def _read_git_config_value(key: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - git config key is selected from a fixed tuple; no shell.
            ["git", "config", key],  # noqa: S607 - git is a trusted host CLI and argv shape is constrained.
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to read git config", key=key, err=str(exc))
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def _read_git_identity() -> tuple[str | None, str | None]:
    """Read git user.name and user.email from the host's git config."""
    name = email = None
    for key in ("user.name", "user.email"):
        value = _read_git_config_value(key)
        if value is None:
            continue
        if key == "user.name":
            name = value
        else:
            email = value
    return name, email


_BASE_NO_PROXY_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

_workspace_env_vars: Callable[..., dict[str, str]] | None = None


@runtime_checkable
class _Gateway(Protocol):
    base_url: str
    key: str

    def has_provider(self, provider: str) -> bool: ...


def configure_workspace_environment(
    workspace_env_vars: Callable[..., dict[str, str]],
) -> None:
    """Install the selected workspace environment projection at composition."""
    global _workspace_env_vars  # noqa: PLW0603 - one host process owns credential policy composition.
    _workspace_env_vars = workspace_env_vars


def _configured_workspace_environment() -> Callable[..., dict[str, str]]:
    if _workspace_env_vars is None:
        raise RuntimeError("workspace environment has not been configured")
    return _workspace_env_vars


def has_api_credentials() -> bool:
    """Check whether the host gateway can serve an LLM provider."""
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415 - keep lazy import to avoid startup cost and preserve patchability.
        get_gateway,
    )

    gateway = cast("_Gateway | None", get_gateway())
    return gateway is not None and (
        gateway.has_provider("anthropic") or gateway.has_provider("openai")
    )


def _gateway_env_vars(gateway: _Gateway | None) -> dict[str, str]:
    """LLM credentials, routed through the gateway (real keys never enter the container)."""
    env_vars: dict[str, str] = {}
    if gateway is None:
        return env_vars
    if gateway.has_provider("anthropic"):
        env_vars["ANTHROPIC_BASE_URL"] = gateway.base_url
        env_vars["ANTHROPIC_AUTH_TOKEN"] = gateway.key
    if gateway.has_provider("openai"):
        env_vars["OPENAI_BASE_URL"] = gateway.base_url
        env_vars["OPENAI_API_KEY"] = gateway.key
    return env_vars


def _dedupe_csv(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _gateway_no_proxy_hosts(gateway: _Gateway) -> list[str]:
    host = urlparse(gateway.base_url).hostname
    return _dedupe_csv([*_BASE_NO_PROXY_HOSTS, host or ""])


def _merge_no_proxy_hosts(env_vars: dict[str, str], hosts: list[str]) -> None:
    existing: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        existing.extend(env_vars.get(key, "").split(","))
    merged = _dedupe_csv([*existing, *hosts])
    for key in ("NO_PROXY", "no_proxy"):
        env_vars[key] = ",".join(merged)


def _git_identity_env_vars() -> dict[str, str]:
    git_name, git_email = _read_git_identity()
    env_vars: dict[str, str] = {}
    if git_name:
        env_vars["GIT_AUTHOR_NAME"] = git_name
        env_vars["GIT_COMMITTER_NAME"] = git_name
    if git_email:
        env_vars["GIT_AUTHOR_EMAIL"] = git_email
        env_vars["GIT_COMMITTER_EMAIL"] = git_email
    return env_vars


def build_agent_env_vars(
    *,
    is_admin: bool,
    group_folder: str,
    extra_env_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the selected workspace environment without ambient discovery."""
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415 - keep lazy import to avoid startup cost and preserve patchability.
        get_gateway,
    )

    env_vars: dict[str, str] = {}
    gateway = cast("_Gateway | None", get_gateway())
    gateway_env_vars = _gateway_env_vars(gateway)
    env_vars.update(gateway_env_vars)
    if extra_env_vars:
        env_vars.update(extra_env_vars)
    if gateway_env_vars:
        _merge_no_proxy_hosts(env_vars, _gateway_no_proxy_hosts(cast("_Gateway", gateway)))
    env_vars.update(_git_identity_env_vars())
    env_vars.update(
        _configured_workspace_environment()(is_admin=is_admin, group_folder=group_folder)
    )
    return env_vars
