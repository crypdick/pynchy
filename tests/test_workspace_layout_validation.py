"""Public validation of semantic workspace layout and migration declarations."""

import pytest
from pydantic import ValidationError

from pynchy.config.api import WorkspaceMigrationConfig, validate_settings_mapping


def test_semantic_scope_becomes_a_routable_workspace() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"reviewer": {}},
            "workspaces": {
                "engineering": {"scopes": [{"workspace": "reviews", "profiles": ["reviewer"]}]}
            },
        }
    )

    assert settings.workspace_parent("reviews") == "engineering"
    assert settings.workspace_config("reviews").profiles == ["reviewer"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ({"threads": [{"name": "review", "workspace": " ", "profiles": []}]}, "cannot be empty"),
        ({"scopes": [{"workspace": " ", "profiles": ["reviewer"]}]}, "scope cannot be empty"),
        ({"scopes": [{"workspace": "reviews", "profiles": []}]}, "scopes require profiles"),
    ],
)
def test_workspace_layout_rejects_incomplete_semantic_declarations(layout, message) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_settings_mapping({"workspaces": {"engineering": layout}})


@pytest.mark.parametrize(
    "workspaces",
    [
        {
            "engineering": {
                "threads": [{"name": "review", "workspace": "reviews", "profiles": ["reviewer"]}]
            },
            "reviews": {},
        },
        {
            "engineering": {"scopes": [{"workspace": "reviews", "profiles": ["reviewer"]}]},
            "design": {"scopes": [{"workspace": "reviews", "profiles": ["reviewer"]}]},
        },
    ],
)
def test_workspace_layout_rejects_conflicting_semantic_identity(workspaces) -> None:
    with pytest.raises(ValidationError, match="semantic workspace"):
        validate_settings_mapping({"profiles": {"reviewer": {}}, "workspaces": workspaces})


def test_workspace_migration_rejects_blank_destination_names() -> None:
    with pytest.raises(ValidationError, match="targets cannot be empty"):
        WorkspaceMigrationConfig(target_workspace=" ", target_thread="family")


@pytest.mark.parametrize(
    ("source", "migration", "message"),
    [
        (
            "relationships",
            {"target_workspace": "relationships", "target_thread": "family"},
            "cannot target the same workspace",
        ),
        (
            "legacy",
            {"target_workspace": "unknown", "target_thread": "family"},
            "targets unknown workspace",
        ),
        (
            "legacy",
            {"target_workspace": "relationships", "target_thread": "missing"},
            "targets undeclared thread",
        ),
    ],
)
def test_workspace_migration_requires_a_declared_destination(source, migration, message) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_settings_mapping(
            {
                "workspaces": {
                    "relationships": {"threads": [{"name": "family"}]},
                },
                "workspace_migrations": {source: migration},
            }
        )
