"""Tests for composable profile config merge logic."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pynchy.config.merge import ResolvedWorkspaceConfig, merge_workspace_profiles
from pynchy.config.models import ProfileConfig


def _profile(**kwargs) -> ProfileConfig:
    return ProfileConfig(**kwargs)


def test_union_fields_merge_in_profile_order() -> None:
    result = merge_workspace_profiles(
        [
            _profile(prompts=["base"], skills=["python"], tools=["shell"], repo="owner/base"),
            _profile(
                prompts=["research"],
                skills=["web"],
                tools=["search"],
                repo="owner/research",
            ),
        ]
    )

    assert result.prompts == ["base", "research"]
    assert result.skills == ["python", "web"]
    assert result.tools == ["shell", "search"]
    assert result.repo == ["owner/base", "owner/research"]


def test_union_fields_are_deduplicated_with_first_occurrence_order() -> None:
    result = merge_workspace_profiles(
        [
            _profile(prompts=["base", "shared"], skills=["python"], tools=["shell"]),
            _profile(prompts=["shared", "extra"], skills=["python", "web"], tools=["shell"]),
        ]
    )

    assert result.prompts == ["base", "shared", "extra"]
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


def test_boolean_privilege_fields_or_across_profiles() -> None:
    result = merge_workspace_profiles(
        [
            _profile(is_admin=False, contains_secrets=True),
            _profile(is_admin=True, contains_secrets=False),
        ]
    )

    assert result.is_admin is True
    assert result.contains_secrets is True


def test_empty_profile_sequence_resolves_to_empty_defaults() -> None:
    result = merge_workspace_profiles([])

    assert result == ResolvedWorkspaceConfig(
        prompts=[],
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )


def test_result_is_frozen() -> None:
    result = merge_workspace_profiles([])

    with pytest.raises(FrozenInstanceError):
        result.model = "changed"  # type: ignore[misc]
