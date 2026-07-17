"""Credential discovery and environment file writing.

Containers receive the gateway URL and an ephemeral key instead of real
API credentials.  Real keys never leave the host process.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404, RUF100 - credential discovery uses fixed no-shell gh/git argv.
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves credential helpers at runtime.
from urllib.parse import urlparse

from pynchy.config import get_settings
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves credential helpers at runtime.
)
from pynchy.host.container_manager.gateway import (  # noqa: TC001, RUF100 - beartype resolves credential helpers at runtime.
    BuiltinGateway,
    LiteLLMGateway,
)
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Host-side discovery helpers
# ---------------------------------------------------------------------------


def _read_gh_token() -> str | None:
    """Read GitHub token from the host's gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607, RUF100 - gh is a trusted host CLI and argv is fixed.
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to read GitHub token from gh CLI", err=str(exc))
    return None


def _read_git_config_value(key: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603, RUF100 - git config key is selected from a fixed tuple; no shell.
            ["git", "config", key],  # noqa: S607, RUF100 - git is a trusted host CLI and argv shape is constrained.
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


def shell_quote(value: str) -> str:
    """Quote a value for safe inclusion in a shell env file."""
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Env file writer
# ---------------------------------------------------------------------------

_BASE_NO_PROXY_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"


def has_api_credentials() -> bool:
    """Check whether LLM API credentials are available for containers.

    Pure check with no filesystem side effects — use this instead of
    calling :func:`write_env_file` with a dummy group folder.
    """
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415, RUF100 - keep lazy import to avoid startup cost and preserve patchability.
        get_gateway,
    )

    gateway = get_gateway()
    return gateway is not None and (
        gateway.has_provider("anthropic") or gateway.has_provider("openai")
    )


def _gateway_env_vars(gateway: LiteLLMGateway | BuiltinGateway | None) -> dict[str, str]:
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


def _gateway_no_proxy_hosts(gateway: LiteLLMGateway | BuiltinGateway | None) -> list[str]:
    if gateway is None:
        return []
    host = urlparse(gateway.base_url).hostname
    return _dedupe_csv([*_BASE_NO_PROXY_HOSTS, host or ""])


def _merge_no_proxy_hosts(env_vars: dict[str, str], hosts: list[str]) -> None:
    if not hosts:
        return
    existing: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        existing.extend(env_vars.get(key, "").split(","))
    merged = _dedupe_csv([*existing, *hosts])
    for key in ("NO_PROXY", "no_proxy"):
        env_vars[key] = ",".join(merged)


def _gh_token_env_var(s: Settings, *, is_admin: bool, group_folder: str) -> dict[str, str]:
    """GH_TOKEN — admin gets a broad token, non-admin gets a repo-scoped token."""
    if is_admin:
        if s.secrets.gh_token:
            return {"GH_TOKEN": s.secrets.gh_token.get_secret_value()}
        if gh_token := _read_gh_token():
            logger.debug("Using GitHub token from gh CLI")
            return {"GH_TOKEN": gh_token}
        return {}

    # Non-admin: inject repo-scoped token if this workspace has a configured repo.
    from pynchy.host.orchestrator.workspace_config import (  # noqa: PLC0415, RUF100 - keep lazy import to avoid startup cost and preserve patchability.
        load_resolved_config,
    )

    resolved = load_resolved_config(group_folder)
    if resolved and resolved.repo:
        tokens = {
            repo_cfg.token.get_secret_value()
            for slug in resolved.repo
            if (repo_cfg := s.repos.overrides.get(slug)) and repo_cfg.token
        }
        if len(tokens) == 1:
            return {"GH_TOKEN": next(iter(tokens))}
    return {}


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


def _chrome_profiles_env_var(s: Settings, *, is_admin: bool, group_folder: str) -> dict[str, str]:
    """Chrome profiles selected by resolved MCP tool names.

    Admin gets every configured chrome profile; non-admin workspaces get the
    chrome profile suffixes from selected MCP tools such as ``gdrive.personal``.
    """
    if is_admin:
        chrome_profiles = s.chrome_profiles
    else:
        chrome_profiles_set: set[str] = set()
        resolved = s.resolved_workspace_config(group_folder)
        for tool_name in resolved.tools if resolved else []:
            tool = s.tools.get(tool_name)
            if tool is None or tool.type != "mcp" or "." not in tool_name:
                continue
            _, inst_name = tool_name.split(".", 1)
            if inst_name in s.chrome_profiles:
                chrome_profiles_set.add(inst_name)
        chrome_profiles = sorted(chrome_profiles_set)

    if chrome_profiles:
        return {"PYNCHY_CHROME_PROFILES": ",".join(chrome_profiles)}
    return {}


def _agent_context_env_vars(*, is_admin: bool, group_folder: str) -> dict[str, str]:
    env_vars = {
        "PYNCHY_GROUP_FOLDER": group_folder,
        "PYNCHY_IS_ADMIN": "1" if is_admin else "0",
    }
    from pynchy.host.learning.paths import (  # noqa: PLC0415, RUF100 - learning is optional and only supplies mounted agent paths.
        resolve_learning_paths,
    )

    if learning_paths := resolve_learning_paths(group_folder):
        env_vars["PYNCHY_SKILLS_ROOT"] = f"{learning_paths.vault_mount_path}/systems/pynchy/skills"
        env_vars["PYNCHY_PROFILE_SKILLS_ROOT"] = learning_paths.mounted_skills_root
    return env_vars


def build_agent_env_vars(
    *,
    is_admin: bool,
    group_folder: str,
    extra_env_vars: dict[str, str] | None = None,
    include_gh_token: bool = True,
) -> dict[str, str]:
    """Build agent environment variables without writing an env-dir file."""
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415, RUF100 - keep lazy import to avoid startup cost and preserve patchability.
        get_gateway,
    )

    s = get_settings()
    env_vars: dict[str, str] = {}
    gateway = get_gateway()
    gateway_env_vars = _gateway_env_vars(gateway)
    env_vars.update(gateway_env_vars)
    if extra_env_vars:
        env_vars.update(extra_env_vars)
    if gateway_env_vars:
        _merge_no_proxy_hosts(env_vars, _gateway_no_proxy_hosts(gateway))
    # The deterministic runtime must not discover credentials from the host,
    # including credentials held by gh outside its sandboxed HOME directory.
    # NOTE: Keep docs/contributing/new-feature.md in sync.  # temporal-ok: current doc path.
    if include_gh_token and os.environ.get(_RUNTIME_HARNESS_ENV) != "1":
        env_vars.update(_gh_token_env_var(s, is_admin=is_admin, group_folder=group_folder))
    env_vars.update(_git_identity_env_vars())
    env_vars.update(_chrome_profiles_env_var(s, is_admin=is_admin, group_folder=group_folder))
    return env_vars


def write_env_file(
    *,
    is_admin: bool,
    group_folder: str,
    extra_env_vars: dict[str, str] | None = None,
    include_gh_token: bool = True,
) -> Path | None:
    """Write credential env vars for a specific group's container.

    Returns the per-group env dir, or ``None`` if no credentials were found.

    LLM credentials are swapped for a gateway URL + ephemeral key.
    Real API keys never enter the container.

    Non-LLM credentials (GH_TOKEN, git identity) are written directly —
    they are not proxied through the gateway.  OneCLI callers pass proxy env in
    ``extra_env_vars`` and set ``include_gh_token=False`` so raw GitHub tokens
    stay out of the container when OneCLI owns that credential boundary.
    """
    s = get_settings()
    env_dir = s.data_dir / "env" / group_folder
    env_dir.mkdir(parents=True, exist_ok=True)

    env_vars = build_agent_env_vars(
        is_admin=is_admin,
        group_folder=group_folder,
        extra_env_vars=extra_env_vars,
        include_gh_token=include_gh_token,
    )

    if not env_vars:
        logger.warning(
            "No credentials found — containers will fail to authenticate. "
            "Configure an LLM provider in litellm_config.yaml/.env or set "
            "[secrets].openai_api_key / [secrets].anthropic_api_key in config.toml"
        )
        return None

    env_vars.update(_agent_context_env_vars(is_admin=is_admin, group_folder=group_folder))

    logger.debug(
        "Container env prepared",
        group=group_folder,
        is_admin=is_admin,
        vars=list(env_vars.keys()),
    )
    lines = [f"{k}={shell_quote(v)}" for k, v in env_vars.items()]
    (env_dir / "env").write_text("\n".join(lines) + "\n")
    return env_dir
