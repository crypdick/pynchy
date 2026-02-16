"""Credential discovery and environment file writing.

Reads OAuth tokens, GitHub tokens, and git identity from the host,
then writes them into an env file for container consumption.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger


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


def _write_env_file() -> Path | None:
    """Write credential env vars for the container. Returns env dir or None.

    Auto-discovers and writes (each independently):
    - Claude credentials: .env file -> OAuth token from Claude Code
    - GH_TOKEN: .env file -> ``gh auth token``
    - Git identity: ``git config user.name/email`` -> GIT_AUTHOR_NAME, etc.

    # TODO: security hardening -- generate per-container scoped tokens (GitHub App
    # installation tokens or fine-grained PATs) instead of forwarding the host's
    # full gh token. Each container should have least-privilege credentials scoped
    # to only the repos/permissions it needs.
    """
    s = get_settings()
    env_dir = s.data_dir / "env"
    env_dir.mkdir(parents=True, exist_ok=True)

    env_vars: dict[str, str] = {}

    # --- Read secrets from Settings ---
    secret_map = {
        "ANTHROPIC_API_KEY": s.secrets.anthropic_api_key,
        "OPENAI_API_KEY": s.secrets.openai_api_key,
        "GH_TOKEN": s.secrets.gh_token,
        "CLAUDE_CODE_OAUTH_TOKEN": s.secrets.claude_code_oauth_token,
    }
    for env_name, secret_val in secret_map.items():
        if secret_val is not None:
            env_vars[env_name] = secret_val.get_secret_value()

    # --- Auto-discover Claude credentials ---
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env_vars and "ANTHROPIC_API_KEY" not in env_vars:
        token = _read_oauth_token()
        if token:
            env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.debug("Using OAuth token from Claude Code credentials")

    # --- Auto-discover GH_TOKEN ---
    if "GH_TOKEN" not in env_vars:
        gh_token = _read_gh_token()
        if gh_token:
            env_vars["GH_TOKEN"] = gh_token
            logger.debug("Using GitHub token from gh CLI")

    # --- Auto-discover git identity ---
    git_name, git_email = _read_git_identity()
    if git_name:
        env_vars["GIT_AUTHOR_NAME"] = git_name
        env_vars["GIT_COMMITTER_NAME"] = git_name
    if git_email:
        env_vars["GIT_AUTHOR_EMAIL"] = git_email
        env_vars["GIT_COMMITTER_EMAIL"] = git_email

    if not env_vars:
        logger.warning(
            "No credentials found — containers will fail to authenticate. "
            "Run 'claude' to authenticate or set [secrets].anthropic_api_key in config.toml"
        )
        return None

    logger.debug("Container env prepared", vars=list(env_vars.keys()))
    lines = [f"{k}={_shell_quote(v)}" for k, v in env_vars.items()]
    (env_dir / "env").write_text("\n".join(lines) + "\n")
    return env_dir
