"""Credential discovery and environment file writing.

Containers receive the gateway URL and an ephemeral key instead of real
API credentials.  Real keys never leave the host process.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Auto-discovery helpers (host-side only)
# ---------------------------------------------------------------------------


def _read_oauth_token() -> str | None:
    """Read the OAuth access token from Claude Code's credentials.

    Checks (in order):
    1. Legacy ~/.claude/.credentials.json file
    2. macOS keychain (service "Claude Code-credentials")
    """
    # 1. Legacy JSON file
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        try:
            data = json.loads(creds_file.read_text())
            token = data.get("claudeAiOauth", {}).get("accessToken")
            if token:
                return token
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to read legacy credentials file", err=str(exc))

    # 2. macOS keychain
    return _read_oauth_from_keychain()


def _read_oauth_from_keychain() -> str | None:
    """Read OAuth token from the macOS keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


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


def _shell_quote(value: str) -> str:
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
    from pynchy.container_runner.gateway import get_gateway

    gateway = get_gateway()
    return gateway is not None and gateway.has_provider("anthropic")


def _write_env_file(*, is_admin: bool, group_folder: str) -> Path | None:
    """Write credential env vars for a specific group's container.

    Returns the per-group env dir, or ``None`` if no credentials were found.

    LLM credentials are replaced by gateway URL + ephemeral key.
    Real API keys never enter the container.

    Non-LLM credentials (GH_TOKEN, git identity) are written directly —
    they are not proxied through the gateway.
    """
    from pynchy.container_runner.gateway import get_gateway

    s = get_settings()
    env_dir = s.data_dir / "env" / group_folder
    env_dir.mkdir(parents=True, exist_ok=True)

    env_vars: dict[str, str] = {}
    gateway = get_gateway()

    # ------------------------------------------------------------------
    # LLM credentials — routed through the gateway
    # ------------------------------------------------------------------

    if gateway is not None:
        if gateway.has_provider("anthropic"):
            env_vars["ANTHROPIC_BASE_URL"] = gateway.base_url
            env_vars["ANTHROPIC_AUTH_TOKEN"] = gateway.key
        if gateway.has_provider("openai"):
            env_vars["OPENAI_BASE_URL"] = gateway.base_url
            env_vars["OPENAI_API_KEY"] = gateway.key

    # ------------------------------------------------------------------
    # Non-LLM credentials (not proxied)
    # ------------------------------------------------------------------

    # GH_TOKEN — admin gets broad token, non-admin gets repo-scoped token
    if is_admin:
        if s.secrets.gh_token:
            env_vars["GH_TOKEN"] = s.secrets.gh_token.get_secret_value()
        elif gh_token := _read_gh_token():
            env_vars["GH_TOKEN"] = gh_token
            logger.debug("Using GitHub token from gh CLI")
    else:
        # Non-admin: inject repo-scoped token if this workspace has repo_access
        ws_cfg = s.workspaces.get(group_folder)
        if ws_cfg and ws_cfg.repo_access:
            repo_cfg = s.repos.get(ws_cfg.repo_access)
            if repo_cfg and repo_cfg.token:
                env_vars["GH_TOKEN"] = repo_cfg.token.get_secret_value()

    # Git identity
    git_name, git_email = _read_git_identity()
    if git_name:
        env_vars["GIT_AUTHOR_NAME"] = git_name
        env_vars["GIT_COMMITTER_NAME"] = git_name
    if git_email:
        env_vars["GIT_AUTHOR_EMAIL"] = git_email
        env_vars["GIT_COMMITTER_EMAIL"] = git_email

    # Chrome profiles — extract from workspace's mcp_servers list.
    # If a workspace has mcp_servers = ["gdrive.anyscale", "gcal.work"],
    # the profiles are {"anyscale", "work"} (extracted from instance names
    # matching templates that have declared instances).
    ws_cfg = s.workspaces.get(group_folder) if not is_admin else None
    # For admin: expose all chrome_profiles. For non-admin: only attached ones.
    if is_admin:
        chrome_profiles = s.chrome_profiles
    else:
        chrome_profiles_set: set[str] = set()
        if ws_cfg and ws_cfg.mcp_servers:
            for entry in ws_cfg.mcp_servers:
                if "." in entry:
                    # "gdrive.anyscale" → check if "anyscale" is a chrome profile
                    _, inst_name = entry.split(".", 1)
                    if inst_name in s.chrome_profiles:
                        chrome_profiles_set.add(inst_name)
        chrome_profiles = sorted(chrome_profiles_set)
    if chrome_profiles:
        env_vars["PYNCHY_CHROME_PROFILES"] = ",".join(chrome_profiles)

    if not env_vars:
        logger.warning(
            "No credentials found — containers will fail to authenticate. "
            "Run 'claude' to authenticate or set [secrets].anthropic_api_key in config.toml"
        )
        return None

    logger.debug(
        "Container env prepared",
        group=group_folder,
        is_admin=is_admin,
        vars=list(env_vars.keys()),
    )
    lines = [f"{k}={_shell_quote(v)}" for k, v in env_vars.items()]
    (env_dir / "env").write_text("\n".join(lines) + "\n")
    return env_dir
