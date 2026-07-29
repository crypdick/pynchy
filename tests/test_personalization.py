"""Tests for layered deployment personalization."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.config.api import (
    PersonalizationError,
    Settings,
    load_layered_settings_mapping,
    repository_settings_sources,
    validate_litellm_model_names,
    validate_personalization_tree,
    validate_settings_mapping,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_tree(root: Path) -> tuple[Path, Path]:
    defaults = root / "data" / "defaults"
    personalization = root / "data" / "personalization"
    defaults.mkdir(parents=True)
    personalization.mkdir(parents=True)
    (defaults / "pynchy.toml").write_text(
        '[agent]\nname = "Default"\n\n[container]\ntimeout_ms = 1000\n\n[workspaces.pynchy]\n',
        encoding="utf-8",
    )
    (personalization / "pynchy.toml").write_text(
        '[agent]\nname = "Personal"\n',
        encoding="utf-8",
    )
    (personalization / "litellm.yaml").write_text(
        "model_list:\n"
        "  - model_name: gpt-test\n"
        "    litellm_params:\n"
        "      model: openai/gpt-test\n",
        encoding="utf-8",
    )
    return defaults, personalization


def test_layers_defaults_personalization_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT__NAME", "Environment")

    with repository_settings_sources(enabled=True):
        settings = Settings()

    assert settings.agent.name == "Environment"
    assert settings.container.timeout_ms == 1000
    assert settings.gateway.litellm_config == str(
        (tmp_path / "data" / "personalization" / "litellm.yaml").resolve()
    )


def test_changing_a_discriminated_mapping_replaces_the_lower_layer(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    (defaults / "pynchy.toml").write_text(
        '[tools.search]\ntype = "builtin"\nname = "shell"\n',
        encoding="utf-8",
    )
    (personalization / "pynchy.toml").write_text(
        '[tools.search]\ntype = "linear"\nworkspace = "PYN"\n',
        encoding="utf-8",
    )

    mapping = load_layered_settings_mapping(
        tmp_path,
        personalization_root=personalization,
        require_personalization=True,
    )

    assert mapping["tools"]["search"] == {"type": "linear", "workspace": "PYN"}


def test_each_automation_file_becomes_a_job_and_personalization_replaces_default(
    tmp_path: Path,
) -> None:
    defaults, personalization = _write_tree(tmp_path)
    default_automations = defaults / "automations"
    personal_automations = personalization / "automations"
    default_automations.mkdir()
    personal_automations.mkdir()
    (default_automations / "weekly.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "default"\n',
        encoding="utf-8",
    )
    (personal_automations / "prompt.md").write_text("personal prompt", encoding="utf-8")
    (personal_automations / "weekly.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 10 * * 1"\n'
        'workspace = "pynchy"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )

    settings = validate_settings_mapping(validate_personalization_tree(tmp_path, personalization))

    assert settings.jobs["weekly"].schedule == "0 10 * * 1"
    assert settings.jobs["weekly"].prompt_file == str(
        (personal_automations / "prompt.md").resolve()
    )


def test_requires_personalization_settings_and_litellm_files(tmp_path: Path) -> None:
    defaults = tmp_path / "data" / "defaults"
    personalization = tmp_path / "personalization"
    defaults.mkdir(parents=True)
    personalization.mkdir()
    (defaults / "pynchy.toml").write_text("", encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Required settings file is missing"):
        validate_personalization_tree(tmp_path, personalization)

    (personalization / "pynchy.toml").write_text("", encoding="utf-8")
    with pytest.raises(PersonalizationError, match="Required LiteLLM configuration"):
        validate_personalization_tree(tmp_path, personalization)


def test_rejects_automation_and_jobs_name_collision(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[jobs.weekly]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "configured twice"\n',
        encoding="utf-8",
    )
    automations = defaults / "automations"
    automations.mkdir()
    (automations / "weekly.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 10 * * 1"\n'
        'workspace = "pynchy"\nprompt = "automation"\n',
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="collide with"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            require_personalization=True,
        )


def test_rejects_litellm_without_routes(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "litellm.yaml").write_text("model_list: []\n", encoding="utf-8")

    with pytest.raises(PersonalizationError, match="non-empty model_list"):
        validate_personalization_tree(tmp_path, personalization)


def test_rejects_inline_secrets_in_settings_and_litellm(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[gateway]\nmaster_key = "do-not-commit-me"\n',
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match=r"must reference os\.environ"):
        validate_personalization_tree(tmp_path, personalization)

    (personalization / "pynchy.toml").write_text("", encoding="utf-8")
    (personalization / "litellm.yaml").write_text(
        "model_list:\n"
        "  - model_name: gpt-test\n"
        "    litellm_params:\n"
        "      model: openai/gpt-test\n"
        "      api_key: literal-secret\n",
        encoding="utf-8",
    )
    with pytest.raises(PersonalizationError, match="store its value"):
        validate_personalization_tree(tmp_path, personalization)


def test_rejects_symlinks_inside_personalized_skills(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills/test-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test skill.\n---\n",
        encoding="utf-8",
    )
    (skill / "outside").symlink_to(tmp_path / "outside")

    with pytest.raises(PersonalizationError, match="cannot contain symlinks"):
        validate_personalization_tree(tmp_path, personalization)


def test_requires_an_existing_personalization_repository(tmp_path: Path) -> None:
    _write_tree(tmp_path)

    with pytest.raises(PersonalizationError, match="Personalization repository is missing"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=tmp_path / "missing-personalization",
            require_personalization=True,
        )


def test_rejects_automation_with_missing_prompt_file(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    automations = personalization / "automations"
    automations.mkdir()
    (automations / "weekly.toml").write_text(
        "schema_version = 1\n"
        "\n[job]\n"
        'workspace = "pynchy"\n'
        'schedule = "0 9 * * 1"\n'
        'prompt_file = "missing.md"\n',
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="references missing prompt file"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_personalization_override_of_convention_owned_litellm_path(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[gateway]\nlitellm_config = "elsewhere.yaml"\n', encoding="utf-8"
    )

    with pytest.raises(PersonalizationError, match="convention-owned"):
        validate_personalization_tree(tmp_path, personalization)


def test_litellm_model_routes_allow_wildcards_and_reject_missing_models(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    litellm = personalization / "litellm.yaml"
    litellm.write_text(
        "model_list:\n  - model_name: openai/*\n    litellm_params:\n      model: openai/gpt-5\n",
        encoding="utf-8",
    )

    validate_litellm_model_names(litellm, ("openai/gpt-5",))
    with pytest.raises(PersonalizationError, match="missing from LiteLLM"):
        validate_litellm_model_names(litellm, ("anthropic/claude",))
