"""Tests for composable profile config merge logic."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pynchy.config.api import ProfileConfig, merge_workspace_profiles
from pynchy.workspace.api import ResolvedWorkspaceConfig


def _profile(**kwargs) -> ProfileConfig:
    return ProfileConfig(**kwargs)


def test_union_fields_merge_in_profile_order() -> None:
    result = merge_workspace_profiles(
        [
            _profile(skills=["python"], tools=["shell"], repo="owner/base"),
            _profile(
                skills=["web"],
                tools=["search"],
                repo="owner/research",
            ),
        ]
    )

    assert result.skills == ["python", "web"]
    assert result.tools == ["shell", "search"]
    assert result.repo == ["owner/base", "owner/research"]


def test_union_fields_are_deduplicated_with_first_occurrence_order() -> None:
    result = merge_workspace_profiles(
        [
            _profile(skills=["python"], tools=["shell"]),
            _profile(skills=["python", "web"], tools=["shell"]),
        ]
    )

    assert result.skills == ["python", "web"]
    assert result.tools == ["shell"]


def test_later_profile_model_wins() -> None:
    result = merge_workspace_profiles(
        [
            _profile(model="base-model"),
            _profile(),
            _profile(model="specialized-model"),
        ]
    )

    assert result.model == "specialized-model"


def test_later_execution_context_wins() -> None:
    result = merge_workspace_profiles(
        [
            _profile(execution_mode="host", cwd="/srv/base"),
            _profile(execution_mode="container", cwd="/workspace/project"),
        ]
    )

    assert result.execution_mode == "container"
    assert result.cwd == "/workspace/project"


def test_boolean_privilege_fields_or_across_profiles() -> None:
    result = merge_workspace_profiles(
        [
            _profile(is_admin=False, contains_secrets=True),
            _profile(is_admin=True, contains_secrets=False),
        ]
    )

    assert result.is_admin is True
    assert result.contains_secrets is True


def test_later_cop_active_setting_wins() -> None:
    result = merge_workspace_profiles(
        [
            _profile(cop_active=False),
            _profile(),
            _profile(cop_active=True),
        ]
    )

    assert result.cop_active is True


def test_empty_profile_sequence_resolves_to_empty_defaults() -> None:
    result = merge_workspace_profiles([])

    assert result == ResolvedWorkspaceConfig(
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
        cop_active=True,
    )


def test_result_is_frozen() -> None:
    result = merge_workspace_profiles([])

    with pytest.raises(FrozenInstanceError):
        result.model = "changed"  # type: ignore[misc]
