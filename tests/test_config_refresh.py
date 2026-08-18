"""Selective host configuration refresh tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import pynchy.host.orchestrator.app as app_module
from pynchy.config.api import (
    PersonalizationError,
    ProfileConfig,
    Settings,
    WorkspaceConfig,
    automation_projection,
    configuration_source_digest,
    get_settings,
    load_runtime_candidate,
    publish_settings,
    repository_settings_sources,
    restart_fingerprint,
    runtime_policy_changes,
)
from pynchy.config.settings_sources import hermetic_settings_sources
from pynchy.host.orchestrator.api import (
    ConfigRefreshRuntime,
    ConfigRefreshStatus,
    configure_config_refresh_runtime,
    refresh_host_config,
)
from pynchy.host.orchestrator.startup_readiness import StartupReadiness
from pynchy.identifiers import GroupFolder, SessionId
from pynchy.state.api import (
    get_session,
    get_session_security_taint,
    get_workspace_profile,
    init_test_database,
    mark_session_security_taint,
    set_session,
    set_workspace_profile,
)
from pynchy.workspace.api import WorkspaceProfile


class _ConfigRefreshApp(app_module.PynchyApp):
    """Focused app owner for live-configuration publication tests."""

    def __init__(self, readiness: StartupReadiness, scheduler_runtime: object) -> None:
        self.startup_readiness = readiness
        self.workspaces = {}
        self.scheduler_runtime = cast("Any", scheduler_runtime)


@pytest.fixture(autouse=True)
def _enable_runtime_sources(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")

    def publish_candidate(candidate: object, **_kwargs: object) -> None:
        publish_settings(cast("Settings", candidate))

    configure_config_refresh_runtime(
        ConfigRefreshRuntime(
            project_root=Path(),
            apply_candidate=AsyncMock(side_effect=publish_candidate),
            automation_projection=lambda candidate: automation_projection(
                cast("Settings", candidate)
            ),
            configuration_source_digest=lambda _root: configuration_source_digest(Path.cwd()),
            get_settings=get_settings,
            load_runtime_candidate=load_runtime_candidate,
            restart_fingerprint=lambda candidate: restart_fingerprint(cast("Settings", candidate)),
            runtime_policy_changes=lambda published, candidate, folders: runtime_policy_changes(
                cast("Settings", published),
                cast("Settings", candidate),
                folders,
            ),
            workspace_folders=lambda: ("test",),
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


def _write_automation(root: Path, *, prompt: str = "initial prompt") -> Path:
    automation_dir = root / "data/personalization/automations/weekly"
    automation_dir.mkdir(parents=True, exist_ok=True)
    automation_path = automation_dir / "config.toml"
    automation_path.write_text(
        "schema_version = 1\n"
        "\n[job]\n"
        'workspace = "test"\n'
        'schedule = "0 9 * * 1"\n'
        f'prompt = """{prompt}"""\n',
        encoding="utf-8",
    )
    return automation_path


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


def test_runtime_candidate_rejects_missing_litellm_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = Mock(gateway=Mock(litellm_config=None))
    candidate.configured_agent_models.return_value = ()
    load_impl = load_runtime_candidate
    while "PersonalizationPaths" not in load_impl.__globals__:
        load_impl = load_impl.__wrapped__
    load_globals = load_impl.__globals__
    monkeypatch.setitem(
        load_globals, "PersonalizationPaths", Mock(for_project=Mock(return_value=Mock()))
    )
    monkeypatch.setitem(load_globals, "validate_personalization_tree", Mock())
    monkeypatch.setitem(load_globals, "Settings", Mock(return_value=candidate))

    with pytest.raises(PersonalizationError, match="must select a LiteLLM configuration"):
        load_runtime_candidate()


@pytest.mark.parametrize(
    "personalized",
    [
        "[agent\nname = 'broken'\n",
        '[agent]\nmodel = "missing-route"\n',
    ],
)
async def test_invalid_candidate_keeps_published_settings(
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

    result = await refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.INVALID
    assert get_settings() is published


async def test_source_change_during_load_defers_publication(
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

    def publish_candidate(value: object, **_kwargs: object) -> None:
        publish_settings(cast("Settings", value))

    configure_config_refresh_runtime(
        ConfigRefreshRuntime(
            project_root=tmp_path,
            apply_candidate=AsyncMock(side_effect=publish_candidate),
            automation_projection=lambda value: automation_projection(cast("Settings", value)),
            configuration_source_digest=source_digest,
            get_settings=get_settings,
            load_runtime_candidate=lambda: candidate,
            restart_fingerprint=lambda value: restart_fingerprint(cast("Settings", value)),
            runtime_policy_changes=lambda published, value, folders: runtime_policy_changes(
                cast("Settings", published),
                cast("Settings", value),
                folders,
            ),
            workspace_folders=lambda: ("test",),
        )
    )

    result = await refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.DEFERRED
    assert get_settings() is published


async def test_pure_skill_policy_change_publishes_without_restart(
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

    result = await refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.REFRESHED
    assert get_settings() is not published
    assert get_settings().profiles["base"].skills == ["beta"]
    assert get_settings().profiles["base"].denied_skills == ["alpha"]
    assert restart_fingerprint(get_settings()) == applied_hash


@pytest.mark.parametrize(
    ("personalized", "status"),
    [
        (
            "[profiles.base]\ncontains_secrets = true\n",
            ConfigRefreshStatus.RUNTIME_POLICY_REFRESHED,
        ),
        (
            '[workspaces.test]\nmodel_reasoning_effort = "medium"\n',
            ConfigRefreshStatus.RUNTIME_POLICY_REFRESHED,
        ),
        (
            "[container]\ntimeout_ms = 123000\n",
            ConfigRefreshStatus.REFRESHED,
        ),
        (
            '[container]\nimage = "pynchy-agent:next"\n',
            ConfigRefreshStatus.RUNTIME_POLICY_REFRESHED,
        ),
    ],
)
async def test_live_runtime_policy_change_uses_weakest_safe_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    personalized: str,
    status: ConfigRefreshStatus,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    config_path.write_text(personalized, encoding="utf-8")

    result = await refresh_host_config(applied_hash)

    assert result.status is status
    assert get_settings() is not published
    assert restart_fingerprint(get_settings()) == applied_hash


@pytest.mark.parametrize(
    "personalized",
    [
        '[agent]\nname = "Changed"\n',
        '[agent]\nname = "Changed"\n[profiles.base]\nskills = ["beta"]\n',
    ],
)
async def test_restart_sensitive_and_mixed_changes_do_not_publish(
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

    result = await refresh_host_config(applied_hash)

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


def test_restart_fingerprint_ignores_thread_and_scope_model_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()
    settings.workspaces["test"] = WorkspaceConfig.model_validate(
        {
            "profiles": ["base"],
            "threads": [
                {
                    "name": "thread",
                    "workspace": "thread",
                    "profiles": ["base"],
                    "model": "thread-model",
                    "model_reasoning_effort": "medium",
                }
            ],
            "scopes": [
                {
                    "workspace": "scope",
                    "profiles": ["base"],
                    "model": "scope-model",
                    "model_reasoning_effort": "medium",
                }
            ],
        }
    )

    baseline = restart_fingerprint(settings)
    settings.workspaces["test"].threads[0].model = "changed-thread-model"
    settings.workspaces["test"].scopes[0].model = "changed-scope-model"

    assert restart_fingerprint(settings) == baseline


def test_restart_fingerprint_changes_when_a_secret_changes() -> None:
    with hermetic_settings_sources():
        without_secret = Settings(_env_file=None)
        with_secret = Settings(
            _env_file=None,
            secrets={"anthropic_api_key": "test-key"},  # pragma: allowlist secret
        )

    assert restart_fingerprint(with_secret) != restart_fingerprint(without_secret)


def test_runtime_policy_change_targets_only_resolved_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    published.profiles["other"] = ProfileConfig()
    published.workspaces["other"] = WorkspaceConfig(profiles=["other"])
    candidate = published.model_copy(deep=True)
    candidate.profiles["base"].contains_secrets = True

    changes = runtime_policy_changes(published, candidate, ("test", "other"))

    assert changes.affected_workspaces == ("test",)
    assert changes.live_changed is True
    assert restart_fingerprint(candidate) == restart_fingerprint(published)


def test_runtime_policy_changes_ignore_unregistered_workspace_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()

    changes = runtime_policy_changes(settings, settings.model_copy(deep=True), ("missing",))

    assert changes == type(changes)((), False)


def test_configuration_source_digest_tracks_missing_symlink_and_racing_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = configuration_source_digest(tmp_path)
    assert baseline

    defaults = tmp_path / "data/defaults"
    defaults.mkdir(parents=True)
    target = defaults / "target.txt"
    target.write_text("target", encoding="utf-8")
    (defaults / "link.txt").symlink_to(target)
    racing = defaults / "racing.txt"
    racing.write_text("racing", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == racing:
            raise FileNotFoundError(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    changed = configuration_source_digest(tmp_path)

    assert changed != baseline


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


def test_automation_projection_ignores_files_absent_from_loaded_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()
    automations = tmp_path / "data/personalization/automations"
    automations.mkdir()
    (automations / "not-loaded.toml").touch()

    assert automation_projection(settings) == ()


def test_runtime_policy_projection_reads_existing_prompt_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = load_runtime_candidate()
    prompts = tmp_path / "data/defaults/prompts/souls"
    prompts.mkdir(parents=True)
    (prompts / "extra.md").write_text("prompt", encoding="utf-8")

    changes = runtime_policy_changes(settings, settings.model_copy(deep=True), ())

    assert changes.live_changed is False


async def test_automation_prompt_change_reconciles_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    prompt_path = _write_automation(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    applied_hash = restart_fingerprint(published)
    before = automation_projection(published)
    _write_automation(tmp_path, prompt="updated prompt")

    candidate = load_runtime_candidate()
    assert restart_fingerprint(candidate) == applied_hash
    assert automation_projection(candidate) != before

    result = await refresh_host_config(applied_hash)

    assert result.status is ConfigRefreshStatus.AUTOMATIONS_RECONCILED
    assert get_settings() is not published


async def test_automation_candidate_waits_for_startup_and_publishes_after_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    _write_automation(tmp_path)
    monkeypatch.chdir(tmp_path)
    candidate = load_runtime_candidate()
    readiness = StartupReadiness()
    previous_runtime = object()
    app = _ConfigRefreshApp(readiness, previous_runtime)
    calls: list[str] = []

    monkeypatch.setattr(
        app_module,
        "reconcile_automation_jobs",
        AsyncMock(side_effect=lambda *_args: calls.append("jobs")),
    )
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "reconcile_schedules_with_config",
        AsyncMock(side_effect=lambda *_args: calls.append("schedules")),
    )
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "publish_scheduler_config",
        lambda _runtime: calls.append("publish-scheduler"),
    )
    monkeypatch.setattr(app_module, "publish_settings", lambda _settings: calls.append("publish"))

    task = asyncio.create_task(
        app.apply_config_candidate(
            candidate,
            affected_workspaces=(),
            reconcile_automations=True,
        )
    )
    await asyncio.sleep(0)
    assert calls == []

    readiness.mark_ready()
    await task

    assert calls == ["jobs", "schedules", "publish", "publish-scheduler"]
    assert app.scheduler_runtime is not previous_runtime


async def test_failed_automation_reconciliation_keeps_published_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    _write_automation(tmp_path)
    monkeypatch.chdir(tmp_path)
    candidate = load_runtime_candidate()
    readiness = StartupReadiness()
    readiness.mark_ready()
    previous_runtime = object()
    app = _ConfigRefreshApp(readiness, previous_runtime)
    publish = Mock()

    monkeypatch.setattr(app_module, "_scheduler_runtime_config", lambda _settings: object())
    monkeypatch.setattr(app_module, "reconcile_automation_jobs", AsyncMock())
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "reconcile_schedules_with_config",
        AsyncMock(side_effect=RuntimeError("Temporal unavailable")),
    )
    monkeypatch.setattr(app_module, "publish_settings", publish)

    with pytest.raises(RuntimeError, match="Temporal unavailable"):
        await app.apply_config_candidate(
            candidate,
            affected_workspaces=(),
            reconcile_automations=True,
        )

    publish.assert_not_called()
    assert app.scheduler_runtime is previous_runtime


async def test_affected_workspace_retires_session_without_clearing_security_taint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    published = load_runtime_candidate()
    publish_settings(published)
    config_path.write_text(
        "[profiles.base]\ncontains_secrets = true\n",
        encoding="utf-8",
    )
    candidate = load_runtime_candidate()
    await init_test_database()

    profile = WorkspaceProfile(
        jid="test@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
    )
    await set_workspace_profile(profile)
    await set_session(GroupFolder("test"), SessionId("provider-session"))
    await mark_session_security_taint(GroupFolder("test"), secret_tainted=True)

    readiness = StartupReadiness()
    readiness.mark_ready()
    app = _ConfigRefreshApp(readiness, object())
    app.workspaces = {profile.jid: profile}
    app.sessions = {"test": "provider-session"}
    app.session_cleared = set()
    app.plugin_manager = object()
    app.queue = Mock(
        pause_runtime_policy=AsyncMock(),
        destroy_runtime_session=AsyncMock(),
        resume_runtime_policy=Mock(),
    )
    monkeypatch.setattr(app_module, "prepare_context_reset", AsyncMock())
    monkeypatch.setattr(app_module.temporal_scheduler, "publish_scheduler_config", Mock())

    await app.apply_config_candidate(
        candidate,
        affected_workspaces=("test",),
        reconcile_automations=False,
    )

    app.queue.pause_runtime_policy.assert_awaited_once()
    app.queue.destroy_runtime_session.assert_awaited_once()
    app.queue.resume_runtime_policy.assert_called_once()
    assert await get_session(GroupFolder("test")) is None
    assert (await get_session_security_taint(GroupFolder("test"))).secret_tainted is True
    stored = await get_workspace_profile(profile.jid)
    assert stored is not None
    assert stored.security.contains_secrets is True
    assert app.sessions == {}
    assert app.session_cleared == {"test"}
    assert get_settings() is candidate
