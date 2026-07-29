"""Git environments for local and authenticated source-control commands."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - fixed Git config lookup reads host identity only.

_GIT_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "USER",
    }
)
_IDENTITY_DISCOVERY_ENV_ALLOWLIST = _GIT_ENV_ALLOWLIST | {"HOME", "XDG_CONFIG_HOME"}
_GIT_CONFIG_TIMEOUT = 5
_MAX_GIT_IDENTITY_VALUE_LENGTH = 1024


def git_env_without_credentials(*, include_identity: bool = True) -> dict[str, str]:
    """Return local Git environment without ambient credentials or hooks."""
    env = {key: os.environ[key] for key in _GIT_ENV_ALLOWLIST if key in os.environ}
    # Keep host and checkout config inaccessible when Git handles a tree an
    # agent can edit. Identity is copied separately below from two fixed,
    # credential-free global config lookups.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if include_identity:
        env.update(_global_git_identity_environment())
    _append_git_config(
        env,
        (
            ("credential.helper", ""),
            ("core.hooksPath", os.devnull),
        ),
    )
    return env


def git_env_with_token(
    slug: str,
    *,
    inherit_host_environment: bool = True,
) -> dict[str, str] | None:
    """Build an environment for authenticated GitHub remote operations.

    Returns ``None`` if no token is available, so callers fall back to ambient
    credentials. A command-scoped credential helper keeps the token out of
    remote URLs and checkout configuration.
    """
    from pynchy.host.git_ops import (  # noqa: PLC0415 - preserve lazy repo runtime setup.
        repo as git_repo,
    )

    token = git_repo.get_repo_token(slug)
    if not token:
        return None

    env = (
        os.environ.copy()
        if inherit_host_environment
        else git_env_without_credentials(include_identity=False)
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_TOKEN"] = token
    _append_git_config(
        env,
        (
            # An empty helper clears any configured host or checkout helper
            # before the GitHub-scoped helper below supplies this token.
            ("credential.helper", ""),
            ("credential.https://github.com.username", "x-access-token"),
            (
                "credential.https://github.com.helper",
                (
                    f"!f() {{ echo protocol=https; echo host=github.com; "
                    f"echo username=x-access-token; echo password={token}; }}; f"
                ),
            ),
            # A token must win over a read-only SSH deploy key on unattended hosts.
            ("url.https://github.com/.insteadOf", "git@github.com:"),
            ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        ),
    )
    return env


def _global_git_identity_environment() -> dict[str, str]:
    """Read only normal global author identity under a credential-free environment."""
    identity = {}
    for key, author_key, committer_key in (
        ("user.name", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"),
        ("user.email", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"),
    ):
        value = _read_global_git_config(key)
        if value is not None:
            identity[author_key] = value
            identity[committer_key] = value
    return identity


def _read_global_git_config(key: str) -> str | None:
    """Read one fixed global Git config key without ambient process secrets."""
    discovery_env = {
        name: os.environ[name] for name in _IDENTITY_DISCOVERY_ENV_ALLOWLIST if name in os.environ
    }
    discovery_env["GIT_CONFIG_NOSYSTEM"] = "1"
    discovery_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(  # noqa: S603 - fixed Git argv, no shell input.
            ["git", "config", "--global", "--get", key],  # noqa: S607 - trusted Git executable.
            check=False,
            capture_output=True,
            env=discovery_env,
            text=True,
            timeout=_GIT_CONFIG_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value if 0 < len(value) <= _MAX_GIT_IDENTITY_VALUE_LENGTH else None


def _append_git_config(env: dict[str, str], entries: tuple[tuple[str, str], ...]) -> None:
    """Append command-scoped Git config without discarding existing entries."""
    try:
        start = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        start = 0
    for index, (key, value) in enumerate(entries, start=start):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(entries))
