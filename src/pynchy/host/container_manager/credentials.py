"""Credential discovery and environment file writing.

Containers receive the gateway URL and an ephemeral key instead of real
API credentials.  Real keys never leave the host process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pynchy.config import get_settings
from pynchy.config.settings import Settings
from pynchy.host.container_manager.gateway import BuiltinGateway, LiteLLMGateway
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Host-side discovery helpers
# ---------------------------------------------------------------------------


def _read_gh_token() -> str | None:
    """Read GitHub token from the host's gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to read GitHub token from gh CLI", err=str(exc))
    return None


def _read_git_identity() -> tuple[str | None, str | None]:
    """Read git user.name and user.email from the host's git config."""
    name = email = None
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(
                ["git", "config", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                if key == "user.name":
                    name = r.stdout.strip()
                else:
                    email = r.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Failed to read git config", key=key, err=str(exc))
    return name, email


def shell_quote(value: str) -> str:
    """Quote a value for safe inclusion in a shell env file."""
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Env file writer
# ---------------------------------------------------------------------------


def has_api_credentials() -> bool:
    """Check whether LLM API credentials are available for containers.

    Pure check with no filesystem side effects — use this instead of
    calling :func:`_write_env_file` with a dummy group folder.
    """
    from pynchy.host.container_manager.gateway import get_gateway

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


def _gh_token_env_var(s: Settings, *, is_admin: bool, group_folder: str) -> dict[str, str]:
    """GH_TOKEN — admin gets a broad token, non-admin gets a repo-scoped token."""
    if is_admin:
        if s.secrets.gh_token:
            return {"GH_TOKEN": s.secrets.gh_token.get_secret_value()}
        if gh_token := _read_gh_token():
            logger.debug("Using GitHub token from gh CLI")
            return {"GH_TOKEN": gh_token}
        return {}

    # Non-admin: inject repo-scoped token if this workspace has repo_access
    from pynchy.host.orchestrator.workspace_config import load_resolved_config

    resolved = load_resolved_config(group_folder)
    if resolved and resolved.repo_access:
        repo_cfg = s.repos.get(resolved.repo_access)
        if repo_cfg and repo_cfg.token:
            return {"GH_TOKEN": repo_cfg.token.get_secret_value()}
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
    """Chrome profiles — extract from workspace's mcp_servers list.

    If a workspace has mcp_servers = ["gdrive.mycompany", "gcal.work"], the
    profiles are {"mycompany", "work"} (extracted from instance names
    matching templates that have declared instances). Admin gets all
    chrome_profiles; non-admin gets only its attached ones.
    """
    if is_admin:
        chrome_profiles = s.chrome_profiles
    else:
        ws_cfg = s.workspaces.get(group_folder)
        chrome_profiles_set: set[str] = set()
        if ws_cfg and ws_cfg.mcp_servers:
            for entry in ws_cfg.mcp_servers:
                if "." in entry:
                    # "gdrive.mycompany" → check if "mycompany" is a chrome profile
                    _, inst_name = entry.split(".", 1)
                    if inst_name in s.chrome_profiles:
                        chrome_profiles_set.add(inst_name)
        chrome_profiles = sorted(chrome_profiles_set)

    if chrome_profiles:
        return {"PYNCHY_CHROME_PROFILES": ",".join(chrome_profiles)}
    return {}


def _write_env_file(
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
    from pynchy.host.container_manager.gateway import get_gateway

    s = get_settings()
    env_dir = s.data_dir / "env" / group_folder
    env_dir.mkdir(parents=True, exist_ok=True)

    env_vars: dict[str, str] = {}
    env_vars.update(_gateway_env_vars(get_gateway()))
    if extra_env_vars:
        env_vars.update(extra_env_vars)
    if include_gh_token:
        env_vars.update(_gh_token_env_var(s, is_admin=is_admin, group_folder=group_folder))
    env_vars.update(_git_identity_env_vars())
    env_vars.update(_chrome_profiles_env_var(s, is_admin=is_admin, group_folder=group_folder))

    if not env_vars:
        logger.warning(
            "No credentials found — containers will fail to authenticate. "
            "Configure an LLM provider in litellm_config.yaml/.env or set "
            "[secrets].openai_api_key / [secrets].anthropic_api_key in config.toml"
        )
        return None

    logger.debug(
        "Container env prepared",
        group=group_folder,
        is_admin=is_admin,
        vars=list(env_vars.keys()),
    )
    lines = [f"{k}={shell_quote(v)}" for k, v in env_vars.items()]
    (env_dir / "env").write_text("\n".join(lines) + "\n")
    return env_dir
