"""Tests for layered deployment personalization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.config.api import (
    PersonalizationError,
    PromptCatalog,
    Settings,
    load_layered_settings_mapping,
    repository_settings_sources,
    validate_litellm_model_names,
    validate_personalization_configuration,
    validate_personalization_tree,
    validate_settings_mapping,
)


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


def test_each_automation_directory_becomes_a_job_and_personalization_replaces_default(
    tmp_path: Path,
) -> None:
    defaults, personalization = _write_tree(tmp_path)
    default_automations = defaults / "automations"
    personal_automations = personalization / "automations"
    default_automations.mkdir()
    personal_automations.mkdir()
    default_weekly = default_automations / "weekly"
    personal_weekly = personal_automations / "weekly"
    default_weekly.mkdir()
    personal_weekly.mkdir()
    (default_weekly / "config.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "default"\n',
        encoding="utf-8",
    )
    (personal_weekly / "config.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 10 * * 1"\n'
        'workspace = "pynchy"\nprompt = "personal prompt"\n'
        'pre_run_command = "./scripts/gate.py"\n',
        encoding="utf-8",
    )

    settings = validate_settings_mapping(validate_personalization_tree(tmp_path, personalization))

    assert settings.jobs["weekly"].schedule == "0 10 * * 1"
    assert settings.jobs["weekly"].prompt == "personal prompt"
    assert settings.jobs["weekly"].pre_run_cwd == str(personal_weekly.resolve())


def test_legacy_flat_automation_files_still_load(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    automations = personalization / "automations"
    automations.mkdir()
    (automations / "weekly.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "legacy"\n',
        encoding="utf-8",
    )

    mapping = load_layered_settings_mapping(tmp_path, personalization_root=personalization)

    assert mapping["jobs"]["weekly"]["prompt"] == "legacy"


def test_rejects_flat_and_directory_automation_name_collision(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    automations = personalization / "automations"
    directory = automations / "weekly"
    directory.mkdir(parents=True)
    content = (
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "duplicate"\n'
    )
    (automations / "weekly.toml").write_text(content, encoding="utf-8")
    (directory / "config.toml").write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Duplicate automation name"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_full_validation_rejects_invalid_settings(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text("unsupported_setting = true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown config sections"):
        validate_personalization_configuration(tmp_path, personalization)


def test_full_validation_requires_global_and_workspace_model_routes(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[agent]\nmodel = "openai/global"\n\n[workspaces.pynchy]\nmodel = "openai/workspace"\n',
        encoding="utf-8",
    )
    (personalization / "litellm.yaml").write_text(
        "model_list:\n"
        "  - model_name: openai/global\n"
        "    litellm_params:\n"
        "      model: openai/global\n",
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="openai/workspace"):
        validate_personalization_configuration(tmp_path, personalization)


def test_full_validation_accepts_wildcard_global_and_workspace_model_routes(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[agent]\nmodel = "openai/global"\n\n[workspaces.pynchy]\nmodel = "openai/workspace"\n',
        encoding="utf-8",
    )
    (personalization / "litellm.yaml").write_text(
        "model_list:\n"
        "  - model_name: openai/*\n"
        "    litellm_params:\n"
        "      model: openai/gpt-test\n",
        encoding="utf-8",
    )

    settings = validate_personalization_configuration(tmp_path, personalization)

    assert settings.configured_agent_models() == ("openai/global", "openai/workspace")


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


def test_rejects_removed_automation_prompt_file_field(tmp_path: Path) -> None:
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

    with pytest.raises(PersonalizationError, match="prompt_file"):
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


def test_requires_bundled_defaults_and_allows_an_absent_optional_overlay(tmp_path: Path) -> None:
    with pytest.raises(PersonalizationError, match="Bundled defaults directory is missing"):
        load_layered_settings_mapping(tmp_path)

    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").unlink()

    mapping = load_layered_settings_mapping(tmp_path, personalization_root=personalization)

    assert mapping["agent"]["name"] == "Default"


def test_loads_defaults_without_an_optional_personalization_directory(tmp_path: Path) -> None:
    defaults = tmp_path / "data" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "pynchy.toml").write_text('[agent]\nname = "Default"\n', encoding="utf-8")

    mapping = load_layered_settings_mapping(
        tmp_path,
        personalization_root=tmp_path / "missing-personalization",
    )

    assert mapping["agent"]["name"] == "Default"


def test_rejects_non_mapping_jobs_when_automation_is_declared(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    automations = defaults / "automations"
    automations.mkdir()
    (automations / "weekly.toml").write_text(
        "schema_version = 1\n"
        "\n[job]\n"
        'workspace = "pynchy"\n'
        'schedule = "0 9 * * 1"\n'
        'prompt = "Run review"\n',
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="jobs setting must be a mapping"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"jobs": []},
        )


def test_rejects_non_mapping_workspaces_when_workspace_is_declared(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    workspaces = defaults / "workspaces"
    workspaces.mkdir()
    (workspaces / "team.toml").write_text(
        "schema_version = 1\n[workspace]\nprofiles = []\n",
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="workspaces setting must be a mapping"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"workspaces": []},
        )


def test_rejects_malformed_automation_documents(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    automations = defaults / "automations"
    automations.mkdir()
    (automations / "weekly.toml").write_text("schema_version =\n", encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Invalid automation"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_non_mapping_gateway_configuration(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)

    with pytest.raises(PersonalizationError, match="gateway setting must be a mapping"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"gateway": ["not-a-mapping"]},
        )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("model_list: [", "Could not parse LiteLLM configuration"),
        ("- model_name: gpt-test\n", "configuration must be a mapping"),
        ("model_list:\n  - invalid\n", r"model_list\[0\] must be a mapping"),
        (
            'model_list:\n  - model_name: " "\n    litellm_params: {}\n',
            "model_name must be a non-empty string",
        ),
        (
            "model_list:\n  - model_name: gpt-test\n    litellm_params: invalid\n",
            "litellm_params must be a mapping",
        ),
    ],
)
def test_rejects_malformed_litellm_routes(tmp_path: Path, content: str, message: str) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "litellm.yaml").write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match=message):
        validate_personalization_tree(tmp_path, personalization)


@pytest.mark.parametrize(
    ("skill_name", "content", "message"),
    [
        ("missing", None, "Skill is missing SKILL.md"),
        ("named", "---\nname: other\ndescription: Valid.\n---\n", "Skill name must match"),
        (
            "description",
            '---\nname: description\ndescription: ""\n---\n',
            "description must be non-empty",
        ),
        ("tier", '---\nname: tier\ndescription: Valid.\ntier: ""\n---\n', "tier must be non-empty"),
    ],
)
def test_rejects_malformed_personalized_skill_metadata(
    tmp_path: Path, skill_name: str, content: str | None, message: str
) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills" / skill_name
    skill.mkdir(parents=True)
    if content is not None:
        (skill / "SKILL.md").write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match=message):
        validate_personalization_tree(tmp_path, personalization)


def _prompt_catalog() -> PromptCatalog:
    ids = (
        "souls/default",
        "executors/default",
        "reviewers/cop-inbound",
        "reviewers/cop-outbound",
        "reviewers/cop-bash",
        "reviewers/cop-taint",
        "reviewers/learning",
        "reviewers/plan-freshness",
        "executors/delivery",
        "reviewers/review",
    )
    return PromptCatalog(content=dict.fromkeys(ids, "content"), sources={})


def test_loads_workspace_and_pipeline_documents(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[prompts]\ndefault_pipeline = "delivery"\n',
        encoding="utf-8",
    )
    workspaces = personalization / "workspaces"
    pipelines = personalization / "pipelines"
    workspaces.mkdir()
    pipelines.mkdir()
    (workspaces / "team.toml").write_text(
        "schema_version = 1\n"
        '[workspace]\nprofiles = []\nsoul = "souls/default"\npipeline = "delivery"\n',
        encoding="utf-8",
    )
    (pipelines / "delivery.toml").write_text(
        "schema_version = 1\n"
        "[pipeline]\n"
        "[[pipeline.stages]]\n"
        'name = "delivery"\nexecutor = "executors/delivery"\n'
        'reviewers = ["reviewers/review"]\n',
        encoding="utf-8",
    )

    with patch("pynchy.config.personalization.load_prompt_catalog", return_value=_prompt_catalog()):
        mapping = load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            require_personalization=True,
        )

    assert mapping["workspaces"]["team"]["pipeline"] == "delivery"
    assert mapping["pipelines"]["delivery"]["stages"][0]["executor"] == "executors/delivery"


@pytest.mark.parametrize(
    ("directory", "filename", "content", "message"),
    [
        (
            "workspaces",
            "team.toml",
            "schema_version = 1\n[workspace]\nprofiles = [",
            "Invalid workspace",
        ),
        (
            "pipelines",
            "delivery.toml",
            "schema_version = 1\n[pipeline]\nstages = []\n",
            "Invalid pipeline",
        ),
        (
            "automations",
            ".broken.toml",
            "schema_version = 1\n[job]\n",
            "Invalid automation name",
        ),
    ],
)
def test_rejects_invalid_layer_documents(
    tmp_path: Path,
    directory: str,
    filename: str,
    content: str,
    message: str,
) -> None:
    _, personalization = _write_tree(tmp_path)
    target = personalization / directory
    target.mkdir()
    (target / filename).write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match=message):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_colliding_workspace_and_pipeline_layers(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    for root, name, content in (
        (
            defaults / "workspaces",
            "same.toml",
            "schema_version = 1\n[workspace]\nprofiles = []\n",
        ),
        (
            personalization / "workspaces",
            "same.toml",
            "schema_version = 1\n[workspace]\nprofiles = []\n",
        ),
    ):
        root.mkdir(exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Workspace files must be globally unique"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_colliding_pipeline_layers(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    content = (
        "schema_version = 1\n[pipeline]\n[[pipeline.stages]]\n"
        "name = 'interactive'\nexecutor = 'executors/default'\n"
    )
    for root in (defaults / "pipelines", personalization / "pipelines"):
        root.mkdir()
        (root / "same.toml").write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Pipeline files must be globally unique"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_non_mapping_pipelines_when_pipeline_is_declared(tmp_path: Path) -> None:
    defaults, personalization = _write_tree(tmp_path)
    pipelines = defaults / "pipelines"
    pipelines.mkdir()
    (pipelines / "team.toml").write_text(
        "schema_version = 1\n[pipeline]\n[[pipeline.stages]]\n"
        "name = 'interactive'\nexecutor = 'executors/default'\n",
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="pipelines setting must be a mapping"):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"pipelines": []},
        )


def test_rejects_invalid_workspace_filename(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    workspaces = personalization / "workspaces"
    workspaces.mkdir()
    (workspaces / ".broken.toml").write_text("", encoding="utf-8")

    with pytest.raises(PersonalizationError, match="Invalid workspace filename"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_unknown_selected_pipeline(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        '[prompts]\ndefault_pipeline = "missing"\n',
        encoding="utf-8",
    )
    with (
        patch("pynchy.config.personalization.load_prompt_catalog", return_value=_prompt_catalog()),
        pytest.raises(PersonalizationError, match="Selected pipeline names do not resolve"),
    ):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)


def test_rejects_invalid_prompt_configuration(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    with (
        patch("pynchy.config.personalization.load_prompt_catalog", return_value=_prompt_catalog()),
        pytest.raises(PersonalizationError, match="Invalid prompt configuration"),
    ):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"prompts": {"default_soul": "invalid"}},
        )


def test_rejects_missing_prompt_ids(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    with (
        patch("pynchy.config.personalization.load_prompt_catalog", return_value=_prompt_catalog()),
        pytest.raises(PersonalizationError, match="Required prompt IDs do not resolve"),
    ):
        load_layered_settings_mapping(
            tmp_path,
            personalization_root=personalization,
            personalization_settings={"prompts": {"default_soul": "souls/missing"}},
        )


def test_rejects_invalid_yaml_frontmatter_in_skill(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills" / "broken"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: [broken\n---\ncontent\n",
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="invalid YAML frontmatter"):
        validate_personalization_tree(tmp_path, personalization)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("plain text", "invalid YAML frontmatter"),
        ("---\n- item\n---\n", "frontmatter must be a mapping"),
    ],
)
def test_rejects_invalid_skill_frontmatter_shape(
    tmp_path: Path, content: str, message: str
) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills" / "broken"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content, encoding="utf-8")

    with pytest.raises(PersonalizationError, match=message):
        validate_personalization_tree(tmp_path, personalization)


def test_reports_skill_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills" / "unreadable"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("content", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "SKILL.md":
            raise OSError("boom")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(PersonalizationError, match="Could not read skill"):
        validate_personalization_tree(tmp_path, personalization)


def test_allows_valid_skill_without_optional_tier(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    skill = personalization / "skills" / "valid"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: valid\ndescription: Valid skill.\n---\ncontent\n",
        encoding="utf-8",
    )

    validate_personalization_tree(tmp_path, personalization)


def test_allows_environment_key_suffix(tmp_path: Path) -> None:
    _, personalization = _write_tree(tmp_path)
    (personalization / "pynchy.toml").write_text(
        f'[gateway]\n{"api" + "_key_env"} = "ENVIRONMENT_VARIABLE"\n', encoding="utf-8"
    )

    validate_personalization_tree(tmp_path, personalization)
