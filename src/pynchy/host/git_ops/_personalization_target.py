"""Trusted Git target resolution for independent personalization publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pynchy.host.git_ops.repo import github_slug_from_origin
from pynchy.host.git_ops.utils import (
    git_env_with_token,
    git_env_without_credentials,
    run_git,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    import subprocess


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    """Trusted remote identity and constrained Git environment for one checkout."""

    main: str
    remote_url: str
    env: dict[str, str]


def run_personalization_git(
    *args: str,
    personalization_root: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git without forwarding ambient host credentials into personalization."""
    return run_git(
        *args,
        cwd=personalization_root,
        env=env or git_env_without_credentials(),
        inherit_env=False,
    )


def resolve_publication_target(personalization_root: Path) -> PublicationTarget | None:
    """Resolve one GitHub origin and its default branch before publication."""
    # Origin is host-operator metadata. Non-admin containers receive only skills.
    # Admin raw host-repository and direct-host access stay operator-trusted.
    safe_env = git_env_without_credentials()
    origin = run_personalization_git(
        "remote", "get-url", "origin", personalization_root=personalization_root, env=safe_env
    )
    slug = github_slug_from_origin(origin.stdout) if origin.returncode == 0 else None
    if slug is None:
        _log_failure("origin")
        return None

    branch = run_personalization_git(
        "branch", "--show-current", personalization_root=personalization_root, env=safe_env
    )
    local_main = _origin_default_branch(personalization_root, safe_env)
    if branch.returncode != 0 or local_main is None:
        _log_failure("branch" if branch.returncode != 0 else "origin HEAD")
        return None
    return _authenticated_publication_target(
        personalization_root,
        slug,
        branch.stdout.strip(),
        local_main,
        safe_env,
    )


def publication_target_is_current(
    personalization_root: Path,
    target: PublicationTarget,
) -> bool:
    """Confirm the canonical remote still advertises this target's default branch."""
    if _remote_default_branch(personalization_root, target.remote_url, target.env) == target.main:
        return True
    _log_failure("origin default branch")
    return False


def _authenticated_publication_target(
    personalization_root: Path,
    slug: str,
    branch: str,
    local_main: str,
    safe_env: dict[str, str],
) -> PublicationTarget | None:
    token_env = git_env_with_token(slug, inherit_host_environment=False)
    if token_env is None:
        logger.warning("Personalization publication requires host GitHub authentication")
        return None
    remote_url = _github_remote_url(slug)
    main = _remote_default_branch(personalization_root, remote_url, token_env)
    # Local origin/HEAD can be stale after a remote default-branch change.
    if main is None or main != local_main:
        _log_failure("origin default branch")
        return None
    if branch != main:
        logger.warning("Personalization repository must be checked out on its origin main branch")
        return None
    tracking_branch = run_personalization_git(
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{main}",
        personalization_root=personalization_root,
        env=safe_env,
    )
    if tracking_branch.returncode != 0:
        _log_failure("origin branch")
        return None
    return PublicationTarget(main=main, remote_url=remote_url, env=token_env)


def _origin_default_branch(
    personalization_root: Path,
    safe_env: dict[str, str],
) -> str | None:
    """Return verified branch named by local origin/HEAD, or fail closed."""
    origin_head = run_personalization_git(
        "symbolic-ref",
        "--quiet",
        "refs/remotes/origin/HEAD",
        personalization_root=personalization_root,
        env=safe_env,
    )
    main = _branch_from_ref(
        origin_head.stdout.strip(), "refs/remotes/origin/", personalization_root
    )
    return main if origin_head.returncode == 0 else None


def _remote_default_branch(
    personalization_root: Path,
    remote_url: str,
    token_env: dict[str, str],
) -> str | None:
    """Return default branch advertised by canonical GitHub remote."""
    remote_head = run_personalization_git(
        "ls-remote",
        "--symref",
        remote_url,
        "HEAD",
        personalization_root=personalization_root,
        env=token_env,
    )
    refs = [
        line.removeprefix("ref: ").removesuffix("\tHEAD")
        for line in remote_head.stdout.splitlines()
        if line.startswith("ref: ") and line.endswith("\tHEAD")
    ]
    if remote_head.returncode != 0 or len(refs) != 1:
        return None
    return _branch_from_ref(refs[0], "refs/heads/", personalization_root)


def _branch_from_ref(ref: str, prefix: str, personalization_root: Path) -> str | None:
    main = ref.removeprefix(prefix)
    if not main or ref != f"{prefix}{main}":
        return None
    valid_branch = run_personalization_git(
        "check-ref-format",
        "--branch",
        main,
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    return main if valid_branch.returncode == 0 and valid_branch.stdout.strip() == main else None


def _github_remote_url(slug: str) -> str:
    """Build canonical HTTPS endpoint from a validated GitHub repository slug."""
    return f"https://github.com/{slug}.git"


def _log_failure(action: str) -> None:
    logger.warning("Could not persist personalization repository", action=action)
