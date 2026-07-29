# Selective Personalization Refresh Implementation Plan

> **Status: Superseded — goal delivered.** The
> [affected-workspace runtime policy refresh](../future-work/workspace-runtime-policy-refresh.md)
> implemented the skill-policy outcome through validated candidate publication
> instead of the hash split and settings-cache reset proposed here. Skill grants
> and denials now refresh before the next turn without restarting Pynchy.
>
> The recipe below records the superseded design. Its step headings are
> historical, not outstanding work.

**Goal:** Apply personalized profile skill grants and denials before the next agent turn without restarting Pynchy, while preserving restarts for every other settings change.

**Architecture:** Split the existing personalization fingerprint into a restart-sensitive hash and a narrow live skill-policy hash. The Temporal host-sync activity validates and publishes personalization first, starts a deploy when the restart hash changes, and otherwise resets the cached settings when only the skill-policy hash changes. Warm sessions already refresh their generated skill registries before each turn; the app must resolve that selection from current settings instead of its startup snapshot.

**Tech Stack:** Python 3.13, stdlib `tomllib` and `hashlib`, Pydantic settings, Temporal activities, pytest, MkDocs, prek.

---

## Scope

This superseded plan proposed hot reload for only these personalized fields:

```toml
[profiles.<name>]
skills = [...]
denied_skills = [...]
```

Profile addition, deletion, or rename remains restart-sensitive, even when the
profile contains only skill fields. For the current field matrix, see
[Affected-workspace runtime policy refresh](../future-work/workspace-runtime-policy-refresh.md).

Skill file content under `data/personalization/skills/` and prompt content already refresh without a restart. This plan does not change those paths.

The bounded follow-up work has these outcomes:

- [Automation hot reconciliation](../future-work/automation-hot-reconciliation.md)
  implements live updates for agent jobs, host cron jobs, and Temporal
  schedules.
- [Workspace topology hot reconciliation](../future-work/workspace-topology-hot-reconciliation.md)
  remains future work for workspace registrations, threads, profile
  assignments, admin identity, and removals.
- [Affected-workspace runtime policy refresh](../future-work/workspace-runtime-policy-refresh.md)
  implements next-turn refresh and affected-session retirement while retaining
  host restarts for process-wide policy.

The topology brief remains not implementation-ready. Promote it to a dated plan
only after its entry criteria are resolved.

## File map

| File | Responsibility |
|---|---|
| `src/pynchy/deployments.py` | Carry the two semantic configuration hashes across package boundaries. |
| `src/pynchy/host/git_ops/sync_poll.py` | Build deterministic restart and skill-policy fingerprints from the personalization tree. |
| `src/pynchy/host/git_ops/api.py` | Export the curated hash reader used by the composition root. |
| `src/pynchy/host/orchestrator/temporal/git_sync.py` | Persist the live-policy baseline, fail closed on invalid personalization, and choose restart versus settings refresh. |
| `src/pynchy/host/orchestrator/workspace_config.py` | Resolve the current workspace skill selection without a startup settings snapshot. |
| `src/pynchy/host/orchestrator/api.py` | Export the workspace skill-selection use case. |
| `src/pynchy/host/orchestrator/app.py` | Wire current hash and settings-reset capabilities into Temporal and skill activation. |
| `tests/conftest.py` | Keep the shared runtime composition fixture complete. |
| `tests/test_git_sync.py` | Lock down restart-sensitive versus live-policy hashing. |
| `tests/test_temporal_git_sync.py` | Lock down selective refresh, restart precedence, legacy state, and validation failure behavior. |
| `tests/test_workspace_config.py` | Prove skill selection reads the current settings provider on every call. |
| `docs/architecture/git-sync.md` | Document the drift classifier and configured polling interval. |
| `docs/architecture/workspaces.md` | Remove the blanket restart statement for skill policy. |
| `docs/usage/personalization.md` | Tell operators which personalization edits apply before the next turn. |

### Task 1: Split restart and workspace skill-policy fingerprints

**Files:**

- Modify: `src/pynchy/deployments.py`
- Modify: `src/pynchy/host/git_ops/sync_poll.py:219-251`
- Modify: `src/pynchy/host/git_ops/api.py:26-111`
- Test: `tests/test_git_sync.py:760-830`

#### Step 1: Write the failing hash-classification tests

Add these tests to `TestHashConfigFiles` in `tests/test_git_sync.py`:

```python
def test_profile_skill_policy_change_does_not_change_restart_hash(self, git_env: dict):
    project = git_env["project"]
    config_path = project / "data" / "personalization" / "pynchy.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
[profiles.dev]
model = "before"
skills = ["core"]
denied_skills = []
""",
        encoding="utf-8",
    )
    before = sync_poll.get_host_config_hashes()

    config_path.write_text(
        """
[profiles.dev]
model = "before"
skills = ["core", "remember-routing"]
denied_skills = ["blocked-skill"]
""",
        encoding="utf-8",
    )
    after = sync_poll.get_host_config_hashes()

    assert before.restart == after.restart
    assert before.workspace_skill_policy != after.workspace_skill_policy


def test_other_profile_change_still_changes_restart_hash(self, git_env: dict):
    project = git_env["project"]
    config_path = project / "data" / "personalization" / "pynchy.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[profiles.dev]\nmodel = "before"\nskills = ["core"]\n',
        encoding="utf-8",
    )
    before = sync_poll.get_host_config_hashes()

    config_path.write_text(
        '[profiles.dev]\nmodel = "after"\nskills = ["core"]\n',
        encoding="utf-8",
    )
    after = sync_poll.get_host_config_hashes()

    assert before.restart != after.restart
    assert before.workspace_skill_policy == after.workspace_skill_policy


def test_adding_skill_only_profile_still_changes_restart_hash(self, git_env: dict):
    project = git_env["project"]
    config_path = project / "data" / "personalization" / "pynchy.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[profiles.dev]\nskills = ["core"]\n',
        encoding="utf-8",
    )
    before = sync_poll.get_host_config_hashes()

    config_path.write_text(
        """
[profiles.dev]
skills = ["core"]

[profiles.new-profile]
skills = ["remember-routing"]
""",
        encoding="utf-8",
    )
    after = sync_poll.get_host_config_hashes()

    assert before.restart != after.restart
    assert before.workspace_skill_policy != after.workspace_skill_policy
```

#### Step 2: Run the tests and verify the new API is missing

Run:

```bash
uv run pytest -q tests/test_git_sync.py -k "profile_skill_policy or other_profile_change or adding_skill_only_profile"
```

Expected: three failures with `AttributeError: module 'pynchy.host.git_ops.sync_poll' has no attribute 'get_host_config_hashes'`.

#### Step 3: Add the cross-package semantic value

Add this immediately after `DeployRevision` in `src/pynchy/deployments.py`:

```python
@dataclass(frozen=True)
class HostConfigHashes:
    """Restart and live-refresh identities for effective host configuration."""

    restart: str
    workspace_skill_policy: str
```

#### Step 4: Replace the single raw personalization hash with two projections

Add these imports in `src/pynchy/host/git_ops/sync_poll.py`:

```python
import tomllib
from collections.abc import Mapping

from pynchy.deployments import HostConfigHashes
```

Replace `_hash_config_files()` and `get_deploy_config_hash()` with:

```python
_LIVE_PROFILE_KEYS = frozenset({"denied_skills", "skills"})


def _canonical_toml_value(value: object) -> object:
    """Return a deterministic, type-preserving value suitable for hashing."""
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _canonical_toml_value(child))
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if isinstance(value, list):
        return tuple(_canonical_toml_value(child) for child in value)
    return value


def _personalization_settings_projections(path: Path) -> tuple[object, object]:
    """Split personalized TOML into restart and live skill-policy projections."""
    if not path.is_file():
        return "__missing__", "__missing__"

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    restart_projection = dict(parsed)
    policy_projection: dict[str, dict[str, object]] = {}
    profiles = parsed.get("profiles")
    if not isinstance(profiles, Mapping):
        return restart_projection, policy_projection

    restart_profiles: dict[str, object] = {}
    for profile_name, profile in profiles.items():
        if not isinstance(profile, Mapping):
            restart_profiles[str(profile_name)] = profile
            continue
        profile_mapping = {str(key): value for key, value in profile.items()}
        restart_profiles[str(profile_name)] = {
            key: value
            for key, value in profile_mapping.items()
            if key not in _LIVE_PROFILE_KEYS
        }
        live_values = {
            key: profile_mapping[key]
            for key in sorted(_LIVE_PROFILE_KEYS)
            if key in profile_mapping
        }
        if live_values:
            policy_projection[str(profile_name)] = live_values

    # Retaining empty profile tables makes profile add/remove/rename restart-sensitive.
    restart_projection["profiles"] = restart_profiles
    return restart_projection, policy_projection


def get_host_config_hashes() -> HostConfigHashes:
    """Return restart-sensitive and live workspace-skill configuration hashes."""
    restart_hash = hashlib.sha256()
    policy_hash = hashlib.sha256()
    project_root = _configured_git_sync_runtime().project_root
    defaults = project_root / "data" / "defaults"
    personalization = project_root / "data" / "personalization"
    personalization_settings = personalization / "pynchy.toml"

    for path in (
        project_root / ".env",
        defaults / "pynchy.toml",
        personalization / "litellm.yaml",
    ):
        restart_hash.update(path.relative_to(project_root).as_posix().encode())
        restart_hash.update(path.read_bytes() if path.is_file() else b"__missing__")

    restart_projection, policy_projection = _personalization_settings_projections(
        personalization_settings
    )
    relative_settings_path = personalization_settings.relative_to(project_root).as_posix().encode()
    restart_hash.update(relative_settings_path)
    restart_hash.update(repr(_canonical_toml_value(restart_projection)).encode())
    policy_hash.update(relative_settings_path)
    policy_hash.update(repr(_canonical_toml_value(policy_projection)).encode())

    # Automation changes still require startup reconciliation.
    for directory in (defaults / "automations", personalization / "automations"):
        if not directory.is_dir():
            restart_hash.update(
                f"__missing__:{directory.relative_to(project_root).as_posix()}".encode()
            )
            continue
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            restart_hash.update(path.relative_to(project_root).as_posix().encode())
            restart_hash.update(path.read_bytes())

    return HostConfigHashes(
        restart=restart_hash.hexdigest(),
        workspace_skill_policy=policy_hash.hexdigest(),
    )


def get_deploy_config_hash() -> str:
    """Return the effective hash of host configuration that requires restart."""
    return get_host_config_hashes().restart
```

Do not include personalized skill or prompt directories in either hash. Their existing per-turn loaders remain authoritative.

#### Step 5: Delete the obsolete adapter-local config drift helper

Delete `_check_config_drift()` from `src/pynchy/host/git_ops/sync_poll.py`. The
Temporal activity owns config-drift orchestration, and this uncalled helper
would otherwise retain a reference to the removed `_hash_config_files()`.

#### Step 6: Export the combined hash reader

In `src/pynchy/host/git_ops/api.py`, add the reader beside the existing deploy
hash import:

```python
get_host_config_hashes,
```

Add this entry to `__all__`:

```python
"get_host_config_hashes",
```

Keep `get_deploy_config_hash` as the compatibility API used by deployment
admission and HTTP status.

#### Step 7: Run the focused tests

Run:

```bash
uv run pytest -q tests/test_git_sync.py -k "HashConfigFiles"
```

Expected: all `TestHashConfigFiles` tests pass. In particular, the existing automation and public-default tests must continue changing `get_deploy_config_hash()`.

#### Step 8: Commit the fingerprint split

```bash
git add src/pynchy/deployments.py src/pynchy/host/git_ops/sync_poll.py \
  src/pynchy/host/git_ops/api.py tests/test_git_sync.py
git commit -m "feat: classify live workspace skill policy"
```

### Task 2: Make Temporal choose restart or live settings refresh

**Files:**

- Modify: `src/pynchy/host/orchestrator/temporal/git_sync.py:18-318`
- Modify: `src/pynchy/host/orchestrator/app.py:131-163,472-488`
- Modify: `tests/conftest.py:18-80,1063-1078`
- Modify: `tests/test_temporal_git_sync.py:1-184`

#### Step 1: Add test support for explicit personalization outcomes and hashes

In `tests/test_temporal_git_sync.py`, import `Mock`, `get_router_state`, and `HostConfigHashes`:

```python
from unittest.mock import AsyncMock, Mock

from pynchy.deployments import DeployRevision, HostConfigHashes
from pynchy.state import (
    claim_deployment,
    complete_deployment,
    get_router_state,
    init_test_database,
    initialize_deployment_state,
    set_router_state,
)
```

Extend `_RuntimeDeps` so tests can force a validation/push failure:

```python
@dataclass
class _RuntimeDeps:
    """The scheduler-dependency subset used by the git-sync Temporal adapter."""

    workspaces: dict[str, WorkspaceProfile]
    broadcast_host_message: object
    personalization_result: str = "skipped"

    def sync_personalization(self, _project_root: Path) -> str:
        return self.personalization_result
```

#### Step 2: Write the failing selective-refresh tests

Add:

```python
async def test_skill_policy_drift_resets_settings_without_deploy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"same-sha","deployed_sha":"same-sha",'
        '"config_hash":"same-config","workspace_skill_policy_hash":"old-policy",'
        '"local_head":"same-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(
        git_sync,
        "get_host_config_hashes",
        lambda: HostConfigHashes("same-config", "new-policy"),
    )
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    reset = Mock()
    monkeypatch.setattr(git_sync, "reset_settings", reset)
    runtime_deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    start_deploy = AsyncMock()
    monkeypatch.setattr(temporal_scheduler, "start_deploy_workflow", start_deploy)

    assert await git_sync.run_host_git_sync() == "skill_policy_reloaded"

    reset.assert_called_once_with()
    start_deploy.assert_not_awaited()
    saved = json.loads(await get_router_state(git_sync.HOST_STATE_KEY) or "{}")
    assert saved["workspace_skill_policy_hash"] == "new-policy"


async def test_restart_drift_takes_precedence_over_skill_policy_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"same-sha","deployed_sha":"same-sha",'
        '"config_hash":"old-config","workspace_skill_policy_hash":"old-policy",'
        '"local_head":"same-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(
        git_sync,
        "get_host_config_hashes",
        lambda: HostConfigHashes("new-config", "new-policy"),
    )
    monkeypatch.setattr(git_sync, "get_deploy_config_hash", lambda: "new-config")
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "same-sha")
    reset = Mock()
    monkeypatch.setattr(git_sync, "reset_settings", reset)
    runtime_deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    start_deploy = AsyncMock()
    monkeypatch.setattr(temporal_scheduler, "start_deploy_workflow", start_deploy)

    assert await git_sync.run_host_git_sync() == "deploy_started"

    start_deploy.assert_awaited_once()
    reset.assert_not_called()


async def test_failed_personalization_neither_deploys_nor_advances_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    hashes = Mock(return_value=HostConfigHashes("new-config", "new-policy"))
    monkeypatch.setattr(git_sync, "get_host_config_hashes", hashes)
    reset = Mock()
    monkeypatch.setattr(git_sync, "reset_settings", reset)
    runtime_deps = _RuntimeDeps(
        workspaces={},
        broadcast_host_message=AsyncMock(),
        personalization_result="failed",
    )
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    start_deploy = AsyncMock()
    monkeypatch.setattr(temporal_scheduler, "start_deploy_workflow", start_deploy)

    assert await git_sync.run_host_git_sync() == "personalization_failed"

    hashes.assert_not_called()
    reset.assert_not_called()
    start_deploy.assert_not_awaited()
```

#### Step 3: Run the tests and verify the missing runtime behavior

Run:

```bash
uv run pytest -q tests/test_temporal_git_sync.py \
  -k "skill_policy_drift or restart_drift_takes_precedence or failed_personalization"
```

Expected: failures because `get_host_config_hashes`, `reset_settings`, the persisted policy hash, and the new results are not wired.

#### Step 4: Extend the Temporal state and runtime capabilities

In `src/pynchy/host/orchestrator/temporal/git_sync.py`, import the new domain value:

```python
from pynchy.deployments import DeployRevision, HostConfigHashes
```

Change `HostSyncState` to:

```python
@dataclass
class HostSyncState:
    last_origin_sha: str | None
    deployed_sha: str
    config_hash: str
    workspace_skill_policy_hash: str = ""
    local_head: str | None = None
    offered_sha: str = ""
```

Add these fields to `TemporalGitSyncRuntime`, `_runtime`, `configure_temporal_git_sync_runtime()`, and the module-level callable bindings:

```python
get_host_config_hashes: Callable[[], HostConfigHashes]
reset_settings: Callable[[], None]
```

Use these exact additions in the default `_runtime`:

```python
get_host_config_hashes=_unconfigured_runtime,
reset_settings=_unconfigured_runtime,
```

Add both names to the `global` declarations in
`configure_temporal_git_sync_runtime()`:

```python
global get_host_config_hashes, reset_settings  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
```

Bind them after `_runtime = runtime`:

```python
get_host_config_hashes = runtime.get_host_config_hashes
reset_settings = runtime.reset_settings
```

Add the module-level defaults beside `get_deploy_config_hash`:

```python
get_host_config_hashes: Callable[[], HostConfigHashes] = _unconfigured_runtime
reset_settings: Callable[[], None] = _unconfigured_runtime
```

#### Step 5: Load and migrate the policy baseline without a restart

Replace `_load_host_state()` with:

```python
async def _load_host_state(current_hashes: HostConfigHashes) -> HostSyncState:
    settings = get_settings()
    raw = await get_router_state(HOST_STATE_KEY)
    state: HostSyncState | None = None
    if raw:
        try:
            payload = json.loads(raw)
            state = HostSyncState(
                last_origin_sha=payload.get("last_origin_sha"),
                deployed_sha=str(payload.get("deployed_sha", "")),
                config_hash=str(payload.get("config_hash", "")),
                workspace_skill_policy_hash=str(
                    payload.get(
                        "workspace_skill_policy_hash",
                        current_hashes.workspace_skill_policy,
                    )
                ),
                local_head=payload.get("local_head"),
                offered_sha=str(payload.get("offered_sha", "")),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Corrupt Temporal host git-sync state, reinitializing")

    if state is None:
        state = HostSyncState(
            last_origin_sha=await asyncio.to_thread(
                host_get_origin_main_sha, settings.project_root
            ),
            deployed_sha=await asyncio.to_thread(get_local_head_sha, settings.project_root),
            config_hash=current_hashes.restart,
            workspace_skill_policy_hash=current_hashes.workspace_skill_policy,
        )

    deployment = await get_deployment_state()
    if deployment.applied is not None:
        applied_changed = (
            state.deployed_sha != deployment.applied.commit_sha
            or state.config_hash != deployment.applied.config_hash
        )
        state.deployed_sha = deployment.applied.commit_sha
        state.config_hash = deployment.applied.config_hash
        if applied_changed:
            # A completed restart already loaded the current policy.
            state.workspace_skill_policy_hash = current_hashes.workspace_skill_policy
    return state
```

The fallback for a missing `workspace_skill_policy_hash` migrates existing router state to the current policy without causing a one-time false reload.

#### Step 6: Add restart precedence and the live refresh action

Replace `_config_drift_started_deploy()` and add `_reload_skill_policy_if_needed()`:

```python
async def _config_drift_started_deploy(
    state: HostSyncState,
    current_hashes: HostConfigHashes,
    deps: _TemporalGitSyncDeps,
) -> bool:
    if current_hashes.restart == state.config_hash:
        return False
    logger.info("Restart-sensitive config changed, starting Temporal deploy")
    await deps.trigger_deploy(state.deployed_sha, rebuild=False)
    return True


def _reload_skill_policy_if_needed(
    state: HostSyncState,
    current_hashes: HostConfigHashes,
) -> bool:
    if current_hashes.workspace_skill_policy == state.workspace_skill_policy_hash:
        return False
    reset_settings()
    state.workspace_skill_policy_hash = current_hashes.workspace_skill_policy
    logger.info("Workspace skill policy changed, reset settings for next turn")
    return True
```

#### Step 7: Fail closed before hashing invalid personalization

Replace `run_host_git_sync()` with:

```python
@activity.defn(name="run_host_git_sync")
async def run_host_git_sync() -> str:
    """Run one host-repo git sync poll through Temporal."""
    if os.environ.get(_RUNTIME_HARNESS_ENV) == "1":
        _record_tracked_activity_result(HOST_GIT_SYNC_ID, "skipped")
        return "skipped"

    deps = _TemporalGitSyncDeps(_require_scheduler_deps(), reason="host_git_sync")
    settings = get_settings()
    personalization_result = await asyncio.to_thread(
        deps.sync_personalization,
        settings.project_root,
    )
    if personalization_result == "failed":
        # Invalid or unpublished personalization must not enter either runtime path.
        _record_tracked_activity_result(HOST_GIT_SYNC_ID, "personalization_failed")
        return "personalization_failed"

    current_hashes = await asyncio.to_thread(get_host_config_hashes)
    state = await _load_host_state(current_hashes)
    repo_ctx = _find_pynchy_repo_ctx(tuple(settings.repos.overrides), settings.project_root)
    result = "personalization_pushed" if personalization_result == "pushed" else "idle"

    try:
        if (
            await _config_drift_started_deploy(state, current_hashes, deps)
            or await _check_local_head_drift(
                settings.project_root,
                state,
                repo_ctx,
                deps,
                auto_deploy=settings.scheduler.auto_deploy,
            )
            or await check_origin_drift(
                settings.project_root,
                state,
                repo_ctx,
                deps,
                auto_deploy=settings.scheduler.auto_deploy,
            )
        ):
            result = "deploy_started"
        elif _reload_skill_policy_if_needed(state, current_hashes):
            result = "skill_policy_reloaded"
    finally:
        if result != "deploy_started" and state.deployed_sha and state.config_hash:
            await advance_deployment_baseline(DeployRevision(state.deployed_sha, state.config_hash))
        await _save_host_state(state)

    _record_tracked_activity_result(HOST_GIT_SYNC_ID, result)
    return result
```

This ordering matters: a mixed edit that changes skill policy and any restart-sensitive field starts a deploy and never exposes the mixed settings through live reload.

#### Step 8: Wire both new capabilities at composition

In `src/pynchy/host/orchestrator/app.py`, import `get_host_config_hashes` from `pynchy.host.git_ops.api`. Add these arguments to `TemporalGitSyncRuntime(...)`:

```python
get_host_config_hashes=get_host_config_hashes,
reset_settings=reset_settings,
```

In `tests/conftest.py`, import `get_host_config_hashes` from the Git API and add:

```python
get_host_config_hashes=get_host_config_hashes,
reset_settings=reset_config_settings,
```

to its `TemporalGitSyncRuntime(...)` fixture.

#### Step 9: Update existing Temporal tests to supply the combined hashes

In `test_trigger_deploy_reports_workflow_start_failure_after_rolling_back`,
add:

```python
monkeypatch.setattr(
    git_sync,
    "get_host_config_hashes",
    lambda: HostConfigHashes("new-config", "current-policy"),
)
```

In `test_applied_revision_overrides_stale_sync_snapshot_after_http_deploy`,
add:

```python
monkeypatch.setattr(
    git_sync,
    "get_host_config_hashes",
    lambda: HostConfigHashes("config-b", "current-policy"),
)
```

#### Step 10: Add a legacy-state regression

Add:

```python
async def test_legacy_host_state_adopts_current_skill_policy_without_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"same-sha","deployed_sha":"same-sha",'
        '"config_hash":"same-config","local_head":"same-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(
        git_sync,
        "get_host_config_hashes",
        lambda: HostConfigHashes("same-config", "current-policy"),
    )
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    reset = Mock()
    monkeypatch.setattr(git_sync, "reset_settings", reset)
    runtime_deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)

    assert await git_sync.run_host_git_sync() == "idle"
    reset.assert_not_called()
```

#### Step 11: Run the complete Temporal Git-sync test file

Run:

```bash
uv run pytest -q tests/test_temporal_git_sync.py
```

Expected: all tests pass. The existing rollback test must still prove that failure to start a restart workflow restores the checkout.

#### Step 12: Commit the Temporal classifier

```bash
git add src/pynchy/host/orchestrator/temporal/git_sync.py \
  src/pynchy/host/orchestrator/app.py tests/conftest.py \
  tests/test_temporal_git_sync.py
git commit -m "feat: refresh skill policy without restart"
```

### Task 3: Resolve skill selection from current settings

**Files:**

- Modify: `src/pynchy/host/orchestrator/workspace_config.py:303-314`
- Modify: `src/pynchy/host/orchestrator/api.py:45-53,100-137`
- Modify: `src/pynchy/host/orchestrator/app.py:250-257,840-860`
- Test: `tests/test_workspace_config.py`

#### Step 1: Write the failing current-settings regression

Import the new use case from `pynchy.host.orchestrator.api` in `tests/test_workspace_config.py`:

```python
from pynchy.host.orchestrator.api import resolve_workspace_skill_selection
```

Add:

```python
def test_workspace_skill_selection_reads_current_settings(monkeypatch):
    workspace = {"dev": WorkspaceConfig(profiles=["dev"])}
    before = make_settings(
        profiles={"dev": ProfileConfig(skills=["core"])},
        workspaces=workspace,
    )
    after = make_settings(
        profiles={
            "dev": ProfileConfig(
                skills=["core", "remember-routing"],
                denied_skills=["blocked-skill"],
            )
        },
        workspaces=workspace,
    )
    monkeypatch.setattr(workspace_config, "get_settings", Mock(side_effect=[before, after]))

    assert resolve_workspace_skill_selection("dev") == (("core",), (), ())
    assert resolve_workspace_skill_selection("dev") == (
        ("core", "remember-routing"),
        ("blocked-skill",),
        (),
    )
```

Also add `Mock` to the existing `unittest.mock` import.

#### Step 2: Run the regression and verify the use case is missing

Run:

```bash
uv run pytest -q tests/test_workspace_config.py::test_workspace_skill_selection_reads_current_settings
```

Expected: collection fails because `resolve_workspace_skill_selection` is not exported.

#### Step 3: Add the application-owned resolver

Add this after `load_resolved_config()` in `src/pynchy/host/orchestrator/workspace_config.py`:

```python
def resolve_workspace_skill_selection(
    group_folder: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """Return the current skill and tool selection for one workspace."""
    resolved = load_resolved_config(group_folder)
    if resolved is None:
        return None
    return (
        tuple(resolved.skills),
        tuple(resolved.denied_skills),
        tuple(resolved.tools),
    )
```

Import it in `src/pynchy/host/orchestrator/api.py`:

```python
resolve_workspace_skill_selection,
```

Add this entry to `__all__`:

```python
"resolve_workspace_skill_selection",
```

#### Step 4: Remove the startup settings capture

Import `resolve_workspace_skill_selection` in the `workspace_config` import block in `src/pynchy/host/orchestrator/app.py`.

Delete the nested `workspace_skill_selection()` closure from `_configure_runtime_dependencies()` and change the `SkillActivationRuntime` argument to:

```python
resolve_workspace_skill_selection=resolve_workspace_skill_selection,
```

Keep `tool_skills` captured from startup. Tool declarations remain restart-sensitive, so live policy reload must not update that mapping.

#### Step 5: Run workspace and warm-skill tests

Run:

```bash
uv run pytest -q tests/test_workspace_config.py \
  tests/test_container_runner.py -k "skill"
```

Expected: all selected tests pass, including the existing assertion that a warm turn refreshes personalized skills before IPC.

#### Step 6: Commit the dynamic policy resolver

```bash
git add src/pynchy/host/orchestrator/workspace_config.py \
  src/pynchy/host/orchestrator/api.py src/pynchy/host/orchestrator/app.py \
  tests/test_workspace_config.py
git commit -m "refactor: resolve current workspace skill policy"
```

### Task 4: Document the selective refresh contract

**Files:**

- Modify: `docs/architecture/git-sync.md:18-39`
- Modify: `docs/architecture/workspaces.md:94-97,124-144`
- Modify: `docs/usage/personalization.md:99-123`

#### Step 1: Update the Git-sync drift table

In `docs/architecture/git-sync.md`, replace the opening sentence and config row with:

```markdown
A Temporal schedule runs at `scheduler.git_sync_interval_seconds` (300 seconds
by default) and detects three types of drift:

| Drift type | What triggers it | Action |
|-----------|-----------------|--------|
| **Origin drift** | Remote main has new commits (e.g. pushed from another machine) | Offer the configured admin a fetch-and-upgrade action; with `scheduler.auto_deploy = true`, pull, notify running agents, and deploy eligible source changes |
| **Local HEAD drift** | Local HEAD differs from the SHA at last deploy (e.g. admin agent committed and pushed) | Offer the configured admin an upgrade action; with `scheduler.auto_deploy = true`, deploy eligible source changes |
| **Config drift** | `.env`, restart-sensitive layered `pynchy.toml` fields, `litellm.yaml`, or an automation changed | Trigger restart (no rebuild needed) |
| **Workspace skill policy drift** | `profiles.*.skills` or `profiles.*.denied_skills` changed in personalized `pynchy.toml` | Reset the settings cache; refresh selected skills before the workspace's next turn |
```

Replace the following explanation with:

```markdown
Source-file changes under `src/` trigger a full deploy and rebuild the agent
container when agent-side files changed. Restart-sensitive config changes
trigger a lighter restart. Prompt files, skill files, and personalized profile
skill policy do not restart Pynchy; selected skills refresh into session
registries before the next turn. A mixed `pynchy.toml` edit remains
restart-sensitive when it changes any field beyond `skills` and
`denied_skills`.
```

#### Step 2: Narrow the workspace restart instruction

In `docs/architecture/workspaces.md`, replace lines 94-97 with:

```markdown
Each scheduled run resolves the target workspace's current effective model. To
change a schedule or prompt, edit its automation file. To change a repo mount
or model override, edit `data/personalization/pynchy.toml`; Pynchy restarts
after the host sync detects that change. Changes limited to a profile's
`skills` and `denied_skills` apply before the workspace's next turn without a
restart. No manual database edits are required.
```

In “Personalized Skill Access,” replace “the next session's skill registry” with “the workspace's skill registry before its next turn.”

#### Step 3: Document the operator-facing behavior once

After the paragraph ending at `docs/usage/personalization.md:119`, add:

```markdown
Changes limited to `profiles.<name>.skills` and
`profiles.<name>.denied_skills` in personalized `pynchy.toml` use the same
next-turn refresh path and do not restart Pynchy. Adding, removing, or renaming
a profile—or changing any other settings field—remains restart-sensitive.
```

#### Step 4: Build the documentation

Run:

```bash
uv run mkdocs build --strict
```

Expected: exit 0 with no broken links or strict-mode warnings.

#### Step 5: Commit the contract

```bash
git add docs/architecture/git-sync.md docs/architecture/workspaces.md \
  docs/usage/personalization.md
git commit -m "docs: explain selective personalization refresh"
```

### Task 5: Run repository gates and verify the completed slice

**Files:**

- No new files.

#### Step 1: Run the complete focused suite

```bash
uv run pytest -q tests/test_git_sync.py tests/test_temporal_git_sync.py \
  tests/test_workspace_config.py tests/test_container_runner.py
```

Expected: all tests pass. The baseline before implementation contains 206 tests across these files.

#### Step 2: Run the architecture checker directly

```bash
uv run python -m scripts.prek_hooks.check_architecture_boundaries
```

Expected: exit 0 with no new boundary or public-surface violations.

#### Step 3: Run every repository hook

```bash
uvx prek run --all-files
```

Expected: exit 0. If a formatter changes a file, review the change and rerun this command until it exits cleanly.

#### Step 4: Confirm the final diff stays within scope

```bash
git status --short
git diff main...HEAD --stat
git log --oneline main..HEAD
```

Expected:

- Implementation changes are limited to the files named by Tasks 1-4.
- No automation reconciliation or generic settings hot-reload code exists.
- The planning commits are followed by the four implementation and
  documentation commits described above.

#### Step 5: Use Pynchy's managed delivery path

From the control checkout:

```bash
new-feature merge plan-selective-personalization-refresh
new-feature teardown plan-selective-personalization-refresh
```

Expected: the managed runtime, pre-merge, and post-merge gates pass; the worktree and manifest entry disappear after teardown. Push and production deployment remain explicit follow-up actions.

## Production acceptance criteria

After an authorized push deploys the implementation:

1. Verify the live host runs the exact pushed SHA and reports healthy service, queue, and Temporal state.
2. Record the current process identity and latest deploy event.
3. Persist one real `Grant always` or `Deny always` skill decision in a workspace.
4. Wait for the configured host Git-sync schedule.
5. Verify the log contains `Workspace skill policy changed, reset settings for next turn`.
6. Verify no deploy workflow started and the Pynchy process identity did not change.
7. Send the workspace another turn and verify both generated agent homes reflect the new grant or denial.

Do not claim production completion from unit tests or health status alone; the acceptance check must observe a real policy edit crossing the live host-sync and next-turn refresh paths.
