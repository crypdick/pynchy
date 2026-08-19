"""Personalization-specific command handlers for the Pynchy CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from pynchy.host.git_ops.api import RepoSettings


def _source_checkout_root() -> Path:
    """Return the host source checkout containing this host-only command."""
    return Path(__file__).resolve().parents[2]


def _stdout_line(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _stderr_line(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def validate_personalization(path: Path) -> int:
    """Validate an operator-selected personalization tree."""
    from pynchy.config.api import (  # noqa: PLC0415 - validation command keeps service imports lazy.
        PersonalizationError,
        validate_personalization_configuration,
    )

    try:
        settings = validate_personalization_configuration(Path.cwd(), path)
    except (OSError, PersonalizationError, ValueError) as exc:
        _stderr_line(f"Personalization validation failed: {exc}")
        return 1
    _stdout_line(
        "Personalization valid: "
        f"{path.resolve()} ({len(settings.jobs)} automation(s), "
        f"{len(settings.workspaces)} workspace(s))"
    )
    return 0


def publish_personalization() -> int:
    """Validate and publish only the canonical host personalization checkout."""
    from pynchy.config.api import (  # noqa: PLC0415 - CLI composes the host validator lazily.
        get_settings,
        validate_personalization_configuration,
    )
    from pynchy.host.git_ops.api import (  # noqa: PLC0415 - CLI composes Git's public facade.
        configure_repo_runtime,
        sync_personalization_repo,
    )

    project_root = _source_checkout_root()
    if Path.cwd().resolve() != project_root:
        _stderr_line("Personalization publication must run from the Pynchy checkout root.")
        return 1

    try:
        configure_repo_runtime(
            get_settings=cast("Callable[[], RepoSettings]", get_settings),
            resolve_workspace_config=lambda _group_folder: None,
        )
        result = sync_personalization_repo(
            project_root,
            validate_personalization_configuration,
        )
    except (OSError, ValueError, RuntimeError):
        _stderr_line("Personalization publication failed; check redacted host logs.")
        return 1
    success_message = {
        "pushed": "Personalization published.",
        "updated": "Personalization updated from origin.",
        "idle": "Personalization already matches origin.",
    }.get(result)
    if success_message is not None:
        _stdout_line(success_message)
        return 0
    if result == "skipped":
        _stderr_line(
            "Personalization publication requires an independent data/personalization repository."
        )
        return 1
    _stderr_line("Personalization publication failed; check redacted host logs.")
    return 1
