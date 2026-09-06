"""RepoContext — abstraction for a tracked git repository.

Maps a GitHub slug (owner/repo) to its filesystem paths. Enables worktrees,
sync loops, and mount logic to work identically for pynchy's own repo and any
external repo configured under [repos."owner/repo"] in layered settings.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess  # noqa: S404 - repo helpers use fixed no-shell git/gh argv.
import uuid
from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pynchy.host.paths import AGENT_SOURCE_CONTAINER_ROOT
from pynchy.logger import logger
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,
)

# Warn when a token expires within this many days
_EXPIRY_WARNING_DAYS = 30
_GITHUB_SCP_ORIGIN = re.compile(r"git@github\.com:(?P<path>[^?#]+)", re.IGNORECASE)
_GITHUB_SLUG_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_UNSUPPORTED_GITHUB_ORIGIN = "checkout origin URL is not a supported GitHub HTTPS or SSH URL"


@runtime_checkable
class _SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


@runtime_checkable
class _RepoOverride(Protocol):
    path: Path | str | None
    token: _SecretValue | None


@runtime_checkable
class _ReposConfig(Protocol):
    root: Path | str
    overrides: Mapping[str, _RepoOverride]


@runtime_checkable
class _SecretsConfig(Protocol):
    gh_token: _SecretValue | None


@runtime_checkable
class RepoSettings(Protocol):
    repos: _ReposConfig
    secrets: _SecretsConfig
    worktrees_dir: Path


def _unconfigured_settings() -> RepoSettings:
    raise RuntimeError("Repository settings have not been composed")


def _unconfigured_workspace(_group_folder: str) -> ResolvedWorkspaceConfig | None:
    raise RuntimeError("Repository workspace resolution has not been composed")


_get_settings: Callable[[], RepoSettings] = _unconfigured_settings
load_resolved_config: Callable[[str], ResolvedWorkspaceConfig | None] = _unconfigured_workspace


def configure_repo_runtime(
    *,
    get_settings: Callable[[], RepoSettings],
    resolve_workspace_config: Callable[[str], ResolvedWorkspaceConfig | None],
) -> None:
    """Bind repository paths and workspace access at host composition."""
    global _get_settings, load_resolved_config  # noqa: PLW0603 - one host process owns this repository configuration.
    _get_settings = get_settings
    load_resolved_config = resolve_workspace_config


@dataclass(frozen=True)
class RepoContext:
    """All location info for a tracked git repository.

    Attributes:
        slug: GitHub slug, e.g. "crypdick/pynchy".
        root: Absolute path to the repository root on disk.
        worktrees_dir: Base directory for worktrees of this repo,
            i.e. data/worktrees/<owner>/<repo>/.
    """

    slug: str
    root: Path
    worktrees_dir: Path


def _slug_to_parts(slug: str) -> tuple[str, str]:
    """Split "owner/repo" into ("owner", "repo"). Raises ValueError if malformed."""
    parts = slug.split("/")
    if len(parts) != 2 or not all(_GITHUB_SLUG_PART.fullmatch(part) for part in parts):
        msg = f"Invalid repo slug {slug!r}: expected 'owner/repo' format"
        raise ValueError(msg)
    return parts[0], parts[1]


def get_repo_context(slug: str) -> RepoContext | None:
    """Resolve a slug to its RepoContext.

    Per-repo overrides are optional. A profile can name ``repo = "owner/repo"``
    and Pynchy resolves the host checkout to ``repos.root / repo`` unless an
    explicit override supplies a different path or token. Container mounts
    remain owner-qualified via :func:`repo_container_path`.
    """
    s = _get_settings()

    owner, repo_name = _slug_to_parts(slug)
    root = repo_host_root(s, slug)
    if root is None:
        return None
    worktrees_dir = s.worktrees_dir / owner / repo_name
    return RepoContext(slug=slug, root=root, worktrees_dir=worktrees_dir)


def repo_host_root(settings: RepoSettings, slug: str) -> Path | None:
    """Return the host checkout path for a repo slug."""
    repo_cfg = settings.repos.overrides.get(slug)
    if repo_cfg and repo_cfg.path is not None:
        return Path(repo_cfg.path)
    try:
        _, repo_name = _slug_to_parts(slug)
    except ValueError:
        return None
    return Path(settings.repos.root) / repo_name


def repo_container_path(slug: str) -> str:
    """Return the stable in-container mount point for a repo slug."""
    owner, repo_name = _slug_to_parts(slug)
    return f"{AGENT_SOURCE_CONTAINER_ROOT}/{owner}/{repo_name}"


def get_repo_token(slug: str) -> str | None:
    """Resolve the git token for a repo, trying each source in priority order.

    Resolution order:
    1. repos."owner/repo".token — explicit per-repo token (highest priority)
    2. secrets.gh_token — host's broad token (medium priority)
    3. gh auth token — auto-discovered from gh CLI (lowest priority)
    """
    s = _get_settings()
    repo_cfg = s.repos.overrides.get(slug)
    if repo_cfg and repo_cfg.token:
        return repo_cfg.token.get_secret_value()
    if s.secrets.gh_token:
        return s.secrets.gh_token.get_secret_value()
    return _read_gh_token()


def _read_gh_token() -> str | None:
    """Read the GitHub token from the host's authenticated gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607 - gh is a trusted host CLI and argv is fixed.
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to read GitHub token from gh CLI", err=str(exc))
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _sanitize_token(text: str, token: str | None) -> str:
    """Strip tokens from text to avoid leaking credentials in logs."""
    from pynchy.host.git_ops.utils import (  # noqa: PLC0415 - preserve the package's lazy import boundary.
        redact_git_diagnostic,
    )

    return redact_git_diagnostic(text, token=token)


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _github_origin_path(origin_url: str) -> tuple[str | None, str | None]:
    """Parse a safe GitHub HTTPS or SSH origin into its repository path."""
    value = origin_url.strip()
    scp_match = _GITHUB_SCP_ORIGIN.fullmatch(value)
    if scp_match is not None:
        return scp_match.group("path"), None

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None, _UNSUPPORTED_GITHUB_ORIGIN
    scheme = parsed.scheme.casefold()
    error: str | None = None
    if scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            error = "checkout origin URL embeds credentials"
        expected_netloc = "github.com"
    elif scheme == "ssh":
        if parsed.password is not None or (parsed.username or "").casefold() != "git":
            error = "checkout origin URL has unsupported SSH userinfo"
        expected_netloc = "git@github.com"
    else:
        expected_netloc = ""
        error = _UNSUPPORTED_GITHUB_ORIGIN
    if error is None and (
        parsed.netloc.casefold() != expected_netloc or parsed.query or parsed.fragment
    ):
        error = _UNSUPPORTED_GITHUB_ORIGIN
    return (parsed.path.removeprefix("/"), None) if error is None else (None, error)


def github_slug_from_origin(origin_url: str) -> str | None:
    """Return ``owner/repository`` for a supported GitHub origin."""
    repo_path, error = _github_origin_path(origin_url)
    if error is not None or repo_path is None:
        return None
    slug = repo_path.removesuffix(".git")
    try:
        _slug_to_parts(slug)
    except ValueError:
        return None
    return slug


def _origin_identity_error(origin_url: str, expected_slug: str) -> str | None:
    """Return why a GitHub origin cannot represent the configured repository."""
    repo_path, parse_error = _github_origin_path(origin_url)
    if parse_error is not None or repo_path is None:
        return parse_error or _UNSUPPORTED_GITHUB_ORIGIN

    if repo_path.casefold().endswith(".git"):
        repo_path = repo_path[:-4]
    try:
        expected_owner, expected_repo = _slug_to_parts(expected_slug)
        actual_owner, actual_repo = _slug_to_parts(repo_path)
    except ValueError:
        return "checkout origin URL does not contain a GitHub owner/repository path"
    if (actual_owner.casefold(), actual_repo.casefold()) != (
        expected_owner.casefold(),
        expected_repo.casefold(),
    ):
        return "checkout origin does not match configured repository"
    return None


def _repo_readiness_error(repo_root: Path, expected_slug: str) -> str | None:
    """Return why a checkout is unsafe for worktrees, or ``None`` when ready."""
    if not repo_root.is_dir():
        return "checkout path is not a directory"

    from pynchy.host.git_ops.utils import (  # noqa: PLC0415 - preserve the package's lazy import boundary.
        redact_git_diagnostic,
        run_git,
    )

    top_level = run_git("rev-parse", "--show-toplevel", cwd=repo_root)
    if top_level.returncode != 0 or not top_level.stdout.strip():
        detail = redact_git_diagnostic(top_level.stderr or "")
        return detail or "checkout is not a Git worktree"
    if Path(top_level.stdout.strip()).resolve() != repo_root.resolve():
        return "checkout path is nested inside a different Git worktree"

    head = run_git("rev-parse", "--verify", "HEAD^{commit}", cwd=repo_root)
    if head.returncode != 0 or not head.stdout.strip():
        detail = redact_git_diagnostic(head.stderr or "")
        return detail or "checkout has no valid HEAD commit"

    origin = run_git("remote", "get-url", "origin", cwd=repo_root)
    if origin.returncode != 0 or not origin.stdout.strip():
        detail = redact_git_diagnostic(origin.stderr or "")
        return detail or "checkout has no origin remote"
    return _origin_identity_error(origin.stdout, expected_slug)


def _unique_sibling_path(repo_root: Path, marker: str) -> Path:
    while True:
        candidate = repo_root.with_name(f".{repo_root.name}.pynchy-{marker}-{uuid.uuid4().hex}")
        if not _path_present(candidate):
            return candidate


def _remove_staged_checkout(staged_root: Path) -> None:
    """Remove only a uniquely named checkout staged by this process."""
    try:
        if staged_root.is_dir() and not staged_root.is_symlink():
            shutil.rmtree(staged_root)
        elif _path_present(staged_root):
            staged_root.unlink()
    except OSError as exc:
        logger.warning(
            "Could not remove failed staged repository checkout",
            path=str(staged_root),
            error=str(exc),
        )


def _clone_repo_to(repo_ctx: RepoContext, target: Path) -> bool:
    """Clone and validate one auto-managed repository at an unpublished path."""
    from pynchy.host.git_ops.utils import (  # noqa: PLC0415 - preserve the package's lazy import boundary.
        git_env_with_token,
        run_git,
    )

    env = git_env_with_token(repo_ctx.slug)
    token = env.get("GH_TOKEN") if env else None
    clone_url = f"https://github.com/{repo_ctx.slug}"

    logger.info("Cloning repo", slug=repo_ctx.slug, dest=str(target))
    result = run_git(
        "clone",
        clone_url,
        str(target),
        cwd=target.parent,
        env=env,
    )
    if result.returncode != 0:
        logger.error(
            "Failed to clone repo",
            slug=repo_ctx.slug,
            stderr=_sanitize_token(result.stderr or "", token),
        )
        return False

    set_url = run_git(
        "remote",
        "set-url",
        "origin",
        clone_url,
        cwd=target,
    )
    if set_url.returncode != 0:
        logger.error(
            "Failed to normalize cloned repo remote",
            slug=repo_ctx.slug,
            stderr=_sanitize_token(set_url.stderr or "", token),
        )
        return False

    readiness_error = _repo_readiness_error(target, repo_ctx.slug)
    if readiness_error is not None:
        logger.error(
            "Cloned repo failed readiness checks",
            slug=repo_ctx.slug,
            error=readiness_error,
        )
        return False
    return True


def _publish_staged_checkout(repo_ctx: RepoContext, staged_root: Path) -> bool:
    """Publish a verified clone while preserving the invalid checkout."""
    recovery_root: Path | None = None
    try:
        if _path_present(repo_ctx.root):
            recovery_root = _unique_sibling_path(repo_ctx.root, "recovery")
            repo_ctx.root.rename(recovery_root)
        staged_root.rename(repo_ctx.root)
    except OSError as exc:
        if recovery_root is not None and _path_present(recovery_root):
            try:
                recovery_root.rename(repo_ctx.root)
            except OSError as restore_exc:
                logger.critical(
                    "Could not restore preserved repository checkout",
                    slug=repo_ctx.slug,
                    recovery_path=str(recovery_root),
                    error=str(restore_exc),
                )
        logger.error(
            "Could not publish staged repository checkout",
            slug=repo_ctx.slug,
            error=str(exc),
        )
        return False

    if recovery_root is not None:
        logger.warning(
            "Preserved invalid repository checkout before recovery",
            slug=repo_ctx.slug,
            recovery_path=str(recovery_root),
        )
    return True


def ensure_repo_cloned(repo_ctx: RepoContext) -> bool:
    """Ensure an auto-managed checkout is ready for worktree operations.

    An invalid auto-managed checkout yields its path only after a staged clone
    passes readiness checks. The displaced directory moves to a uniquely named
    recovery sibling so Pynchy never deletes unknown or user-owned data.

    Explicit-path repositories remain operator-owned and recovery does not
    modify them.
    """
    readiness_error = (
        _repo_readiness_error(repo_ctx.root, repo_ctx.slug)
        if _path_present(repo_ctx.root)
        else None
    )
    if readiness_error is None and _path_present(repo_ctx.root):
        return True

    settings = _get_settings()
    repo_config = settings.repos.overrides.get(repo_ctx.slug)
    if repo_config is not None and repo_config.path is not None:
        logger.error(
            "Configured repository checkout is not ready",
            slug=repo_ctx.slug,
            path=str(repo_ctx.root),
            error=readiness_error or "checkout does not exist",
        )
        return False

    if readiness_error is not None:
        logger.warning(
            "Auto-managed repository checkout is not ready; staging recovery",
            slug=repo_ctx.slug,
            path=str(repo_ctx.root),
            error=readiness_error,
        )

    repo_ctx.root.parent.mkdir(parents=True, exist_ok=True)
    staged_root = _unique_sibling_path(repo_ctx.root, "clone")
    if not _clone_repo_to(repo_ctx, staged_root):
        _remove_staged_checkout(staged_root)
        return False
    if not _publish_staged_checkout(repo_ctx, staged_root):
        _remove_staged_checkout(staged_root)
        return False

    logger.info("Cloned repo", slug=repo_ctx.slug)
    return True


def resolve_repos_for_group(group_folder: str) -> list[RepoContext]:
    """Return every resolved repo context for a workspace, preserving profile order."""
    resolved = load_resolved_config(group_folder)
    if resolved is None or not resolved.repo:
        return []
    return [repo_ctx for slug in resolved.repo if (repo_ctx := get_repo_context(slug))]


def check_token_expiry(slug: str, token: str) -> None:
    """Check a fine-grained PAT's expiry via the GitHub API.

    Logs a warning if the token expires within _EXPIRY_WARNING_DAYS.
    Logs an error if the token is already expired.
    Silently succeeds if the API call fails (network issues, classic token, etc.).
    """
    from pynchy.host.git_ops.utils import (  # noqa: PLC0415 - preserve the package's lazy import boundary.
        git_env_without_credentials,
    )

    env = git_env_without_credentials(include_identity=False)
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(
            [  # noqa: S607 - gh is the trusted host GitHub CLI.
                "gh",
                "api",
                "/rate_limit",
                "-i",
            ],
            capture_output=True,
            env=env,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.debug("Could not check token expiry", slug=slug, err=str(exc))
        return

    if result.returncode != 0:
        return  # Can't check — might be a classic token or network issue

    for line in result.stdout.splitlines():
        if line.lower().startswith("github-authentication-token-expiration:"):
            expiry_str = line.split(":", 1)[1].strip()
            # GitHub returns the expiry as a UTC timestamp, for example
            # "2024-11-30 09:00:00 UTC".
            expiry = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S %Z").replace(
                tzinfo=datetime.UTC
            )
            now = datetime.datetime.now(datetime.UTC)
            days_left = (expiry - now).days

            if days_left < 0:
                logger.error(
                    "Repo token has EXPIRED — git operations will fail",
                    slug=slug,
                    expired_on=expiry_str,
                )
            elif days_left <= _EXPIRY_WARNING_DAYS:
                logger.warning(
                    "Repo token expiring soon",
                    slug=slug,
                    expires=expiry_str,
                    days_left=days_left,
                )
            else:
                logger.debug(
                    "Repo token expiry OK",
                    slug=slug,
                    days_left=days_left,
                )
            return
