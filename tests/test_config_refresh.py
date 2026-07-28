"""Selective host configuration refresh tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from pynchy.config.api import (
    ProfileConfig,
    Settings,
    configuration_source_digest,
    get_settings,
    load_runtime_candidate,
    publish_settings,
    repository_settings_sources,
    restart_fingerprint,
    skill_policy_projection,
)
from pynchy.host.orchestrator.api import (
    ConfigRefreshRuntime,
    ConfigRefreshStatus,
    configure_config_refresh_runtime,
    refresh_host_config,
)


@pytest.fixture(autouse=True)
def _enable_runtime_sources(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    configure_config_refresh_runtime(
        ConfigRefreshRuntime(
            project_root=Path(),
            configuration_source_digest=lambda _root: configuration_source_digest(Path.cwd()),
            get_settings=get_settings,
            load_runtime_candidate=load_runtime_candidate,
            publish_settings=lambda candidate: publish_settings(cast("Settings", candidate)),
            restart_fingerprint=lambda candidate: restart_fingerprint(cast("Settings", candidate)),
            skill_policy_projection=lambda candidate: skill_policy_projection(
                cast("Settings", candidate)
            ),
        )
    )
    with repository_settings_sources(enabled=True):
        yield


def _write_runtime_tree(root: Path, *, personalized: str = "") -> Path:
    defaults = root / "data/defaults"
    personalization = root / "data/personalization"
    defaults.mkdir(parents=True)
    personalization.mkdir(parents=True)
    (defaults / "pynchy.toml").write_text(
        '[agent]\nname = "Default"\nmodel = "gpt-test"\n'
        '\n[profiles.base]\nskills = ["alpha"]\n'
        '\n[workspaces.test]\nprofiles = ["base"]\n',
        encoding="utf-8",
    )
    (personalization / "pynchy.toml").write_text(personalized, encoding="utf-8")
    (personalization / "litellm.yaml").write_text(
        "model_list:\n"
        "  - model_name: gpt-test\n"
        "    litellm_params:\n"
        "      model: openai/gpt-test\n",
        encoding="utf-8",
    )
    return personalization / "pynchy.toml"


def test_runtime_candidate_preserves_dotenv_and_environment_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    (tmp_path / ".env").write_text("AGENT__NAME=Dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT__NAME", raising=False)

    assert load_runtime_candidate().agent.name == "Dotenv"

    monkeypatch.setenv("AGENT__NAME", "Environment")
    assert load_runtime_candidate().agent.name == "Environment"


@pytest.mark.parametrize(
    "personalized",
    [
        "[agent\nname = 'broken'\n",
        '[agent]\nmodel = "missing-route"\n',
    ],
)
def test_invalid_candidate_keeps_published_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    personalized: str,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    config_path.write_text(personalized, encoding="utf-8")

    result = refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.INVALID
    assert get_settings() is published


def test_source_change_during_load_defers_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    config_path.write_text('[profiles.base]\nskills = ["beta"]\n', encoding="utf-8")
    candidate = load_runtime_candidate()

    source_digest = Mock(side_effect=("before", "after"))
    configure_config_refresh_runtime(
        ConfigRefreshRuntime(
            project_root=tmp_path,
            configuration_source_digest=source_digest,
            get_settings=get_settings,
            load_runtime_candidate=lambda: candidate,
            publish_settings=lambda value: publish_settings(cast("Settings", value)),
            restart_fingerprint=lambda value: restart_fingerprint(cast("Settings", value)),
            skill_policy_projection=lambda value: skill_policy_projection(cast("Settings", value)),
        )
    )

    result = refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.DEFERRED
    assert get_settings() is published


def test_pure_skill_policy_change_publishes_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    config_path.write_text(
        '[profiles.base]\nskills = ["beta"]\ndenied_skills = ["alpha"]\n',
        encoding="utf-8",
    )

    result = refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.REFRESHED
    assert get_settings() is not published
    assert get_settings().profiles["base"].skills == ["beta"]
    assert get_settings().profiles["base"].denied_skills == ["alpha"]
    assert restart_fingerprint(get_settings()) == applied_hash


@pytest.mark.parametrize(
    "personalized",
    [
        '[agent]\nname = "Changed"\n',
        '[agent]\nname = "Changed"\n[profiles.base]\nskills = ["beta"]\n',
    ],
)
def test_restart_sensitive_and_mixed_changes_do_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    personalized: str,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    config_path.write_text(personalized, encoding="utf-8")

    result = refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.RESTART_REQUIRED
    assert result.restart_hash != applied_hash
    assert get_settings() is published


def test_profile_identity_changes_remain_restart_sensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()
    baseline = restart_fingerprint(settings)

    added = settings.model_copy(deep=True)
    added.profiles["other"] = ProfileConfig(skills=["beta"])
    removed = settings.model_copy(deep=True)
    del removed.profiles["base"]
    renamed = settings.model_copy(deep=True)
    renamed.profiles["renamed"] = renamed.profiles.pop("base")

    assert restart_fingerprint(added) != baseline
    assert restart_fingerprint(removed) != baseline
    assert restart_fingerprint(renamed) != baseline


def test_restart_fingerprint_covers_raw_restart_owned_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()
    baseline = restart_fingerprint(settings)

    (tmp_path / ".env").write_text("AGENT__NAME=changed\n", encoding="utf-8")
    assert restart_fingerprint(settings) != baseline
    (tmp_path / ".env").unlink()

    litellm = tmp_path / "data/personalization/litellm.yaml"
    original_litellm = litellm.read_text(encoding="utf-8")
    litellm.write_text(
        "model_list:\n"
        "  - model_name: gpt-test\n"
        "    litellm_params:\n"
        "      model: openai/changed-backend\n",
        encoding="utf-8",
    )
    assert restart_fingerprint(settings) != baseline
    litellm.write_text(original_litellm, encoding="utf-8")

    automation = tmp_path / "data/personalization/automations/prompt.md"
    automation.parent.mkdir()
    automation.write_text("changed prompt", encoding="utf-8")
    assert restart_fingerprint(settings) != baseline
