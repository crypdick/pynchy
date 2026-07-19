"""Tests for workspace-scoped Proton Pass secret-reference injection."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config.models import WorkspaceConfig
from pynchy.host.container_manager import credentials

if TYPE_CHECKING:
    from pathlib import Path


_TEMPLATE_CONFIG = {"proton_pass_env_file": "secrets/review.env"}


def _write_template(root: Path, content: str) -> Path:
    template = root / "secrets" / "review.env"
    template.parent.mkdir()
    template.write_text(content)
    return template


def test_workspace_rejects_absolute_or_parent_secret_template_paths() -> None:
    with pytest.raises(ValidationError, match="relative path"):
        WorkspaceConfig.model_validate({"proton_pass_env_file": "../secrets.env"})
    with pytest.raises(ValidationError, match="relative path"):
        WorkspaceConfig.model_validate({"proton_pass_env_file": "/absolute/secrets.env"})


def test_workspace_secret_template_marks_resolved_workspace_as_secret_bearing(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        workspaces={"review": WorkspaceConfig.model_validate(_TEMPLATE_CONFIG)},
        project_root=tmp_path,
    )

    resolved = settings.resolved_workspace_config("review")

    assert resolved is not None
    assert resolved.contains_secrets is True


def test_proton_pass_resolves_only_template_variable_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(
        tmp_path,
        "FIRST=pass://Personal/Todoist/password\nSECOND=pass://Personal/Database/password\n",
    )
    settings = make_settings(
        workspaces={"review": WorkspaceConfig.model_validate(_TEMPLATE_CONFIG)},
        project_root=tmp_path,
    )
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(command: list[str], **kwargs) -> Mock:
        environment = kwargs["env"]
        calls.append((command, environment.get("PROTON_PASS_AGENT_REASON")))
        return Mock(
            returncode=0,
            stdout=b"FIRST=one\0SECOND=two\0UNRELATED=three\0",
        )

    monkeypatch.setattr(credentials.shutil, "which", lambda _name: "/opt/homebrew/bin/pass-cli")
    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    monkeypatch.setattr(credentials, "get_settings", lambda: settings)
    monkeypatch.setattr(credentials, "_read_git_identity", lambda: (None, None))
    monkeypatch.setattr("pynchy.host.container_manager.gateway.get_gateway", lambda: None)

    result = credentials.build_agent_env_vars(
        is_admin=False,
        group_folder="review",
        include_gh_token=False,
    )

    assert result == {"FIRST": "one", "SECOND": "two"}
    assert calls == [
        (
            [
                "/opt/homebrew/bin/pass-cli",
                "run",
                "--no-masking",
                "--env-file",
                str(tmp_path / "secrets" / "review.env"),
                "--",
                "/usr/bin/env",
                "-0",
            ],
            "Resolve credentials for a Pynchy workspace container",
        )
    ]


def test_proton_pass_failure_does_not_include_process_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_template(tmp_path, "FIRST=pass://Personal/Todoist/password\n")
    settings = make_settings(
        workspaces={"review": WorkspaceConfig.model_validate(_TEMPLATE_CONFIG)},
        project_root=tmp_path,
    )
    monkeypatch.setattr(credentials.shutil, "which", lambda _name: "/opt/homebrew/bin/pass-cli")
    monkeypatch.setattr(
        credentials.subprocess,
        "run",
        lambda *_args, **_kwargs: Mock(
            returncode=1, stdout=b"sensitive-value", stderr=b"sensitive-value"
        ),
    )
    monkeypatch.setattr(credentials, "get_settings", lambda: settings)
    monkeypatch.setattr(credentials, "_read_git_identity", lambda: (None, None))
    monkeypatch.setattr("pynchy.host.container_manager.gateway.get_gateway", lambda: None)

    with pytest.raises(credentials.ProtonPassSecretResolutionError) as exc_info:
        credentials.build_agent_env_vars(
            is_admin=False,
            group_folder="review",
            include_gh_token=False,
        )

    assert "sensitive-value" not in str(exc_info.value)
