# Obsidian Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add v1 automatic learning to Pynchy by mounting the configured Obsidian vault root into learning-enabled agent containers, saving memories by folder convention, loading learned skills from a profile-scoped vault folder, and enqueueing bounded post-turn learning review jobs through a durable IPC-backed queue.

**Architecture:** Host config resolves a vault root plus profile-scoped fallback paths. Container mount construction mounts the vault root as the global memory namespace and syncs learned skill folders from the source profile. The message pipeline enqueues a bounded learning packet only after a clean successful turn. A host-side learning worker claims durable jobs from `data/ipc/learning`, runs a hidden review agent in a synthetic session, and lets that reviewer write notes or skills directly into the mounted vault.

**Tech Stack:** Python 3.12, Pydantic settings, asyncio, existing Pynchy container runner, existing `.claude/skills` sync, filesystem IPC directories, `uv run pytest`, `uv run mkdocs build --strict`.

---

## Design Decisions To Preserve

- The configured vault root is the global memory namespace and is mounted into learning-enabled containers.
- Memory organization is folder-based. Do not require semantic frontmatter on memory notes.
- The v1 fallback namespace is profile-scoped, not workspace-scoped: `systems/pynchy/profiles/{profile}/memory`.
- Learned skills are profile-scoped: `systems/pynchy/profiles/{profile}/skills/<skill-name>/SKILL.md`.
- Workspace configs without a profile use profile name `default`.
- A reviewer writes immediately after a successful turn, but receives a bounded packet instead of the full transcript.
- The queue is durable over process restarts and uses claim, lease, retry, done, and error directories.
- Vault access controls and narrower subdir mounts are not part of this implementation.

## File Map

Create:

- `src/pynchy/host/learning/__init__.py`
- `src/pynchy/host/learning/paths.py`
- `src/pynchy/host/learning/skills.py`
- `src/pynchy/host/learning/queue.py`
- `src/pynchy/host/learning/packets.py`
- `src/pynchy/host/learning/reviewer.py`
- `src/pynchy/host/learning/worker.py`
- `tests/test_learning_paths.py`
- `tests/test_learning_queue.py`
- `tests/test_learning_reviewer.py`
- `tests/test_learning_worker.py`

Modify:

- `src/pynchy/config/models.py`
- `src/pynchy/config/settings.py`
- `src/pynchy/host/container_manager/mounts.py`
- `src/pynchy/host/container_manager/session_prep.py`
- `src/pynchy/host/orchestrator/messaging/pipeline.py`
- `src/pynchy/host/orchestrator/dep_factory.py`
- `src/pynchy/host/orchestrator/lifecycle.py`
- `config-examples/config.toml.EXAMPLE`
- `docs/architecture/container-isolation.md`
- `docs/architecture/memory-and-sessions.md` or the closest existing memory/session page after checking docs nav.
- `mkdocs.yml` only if a new docs page is added.

## Task 1: Config Model And Path Resolver

- [ ] Add failing tests in `tests/test_learning_paths.py`.

Test cases:

- `LearningConfig` defaults to disabled and does not require a vault.
- Enabling learning without `obsidian.vault_root` raises a `LearningConfigError` from the resolver, not at import time.
- A workspace with `profile = "shopping"` resolves profile root to `<vault>/systems/pynchy/profiles/shopping`.
- A workspace without `profile` resolves to `<vault>/systems/pynchy/profiles/default`.
- Profile names are sanitized for path use while preserving the original value for logs and packets.
- `default_profile_root` must be relative and must not escape the vault with `..`.
- `mount_path` must be an absolute container path.

Expected red failures:

```text
ModuleNotFoundError: No module named 'pynchy.host.learning'
pydantic_core._pydantic_core.ValidationError: Extra inputs are not permitted
```

- [ ] Add config models to `src/pynchy/config/models.py`.

Use this shape:

```python
class ObsidianLearningConfig(_StrictModel):
    vault_root: str | None = None
    mount_path: str = "/workspace/vault"
    default_profile_root: str = "systems/pynchy/profiles/{profile}"
    memory_dir_name: str = "memory"
    skills_dir_name: str = "skills"


class LearningConfig(_StrictModel):
    enabled: bool = False
    review_after_turn: bool = True
    queue_poll_interval_seconds: float = 5.0
    lease_seconds: int = 300
    max_attempts: int = 3
    packet_max_chars: int = 12_000
    skill_max_bytes: int = 200_000
    obsidian: ObsidianLearningConfig = ObsidianLearningConfig()
```

Validation:

- `mount_path` starts with `/`.
- `default_profile_root` is a relative path template.
- `default_profile_root` may contain `{profile}`.
- No component in `default_profile_root` is `..`.

- [ ] Add `learning: LearningConfig = LearningConfig()` to `src/pynchy/config/settings.py`.
- [ ] Create `src/pynchy/host/learning/paths.py`.

Core API:

```python
@dataclass(frozen=True)
class LearningPaths:
    profile: str
    profile_slug: str
    vault_root: Path
    vault_mount_path: str
    profile_root: Path
    memory_root: Path
    skills_root: Path
    mounted_profile_root: str
    mounted_memory_root: str
    mounted_skills_root: str


class LearningConfigError(ValueError):
    pass


def profile_name_for_group(group_folder: str) -> str:
    ...


def resolve_learning_paths(group_folder: str, *, profile_override: str | None = None) -> LearningPaths | None:
    ...
```

Implementation notes:

- Return `None` when `settings.learning.enabled` is false.
- Expand `~` in `vault_root` and resolve it on the host.
- Do not create directories in the resolver except in the mount or worker code paths that need them.
- Compute mounted paths by joining `settings.learning.obsidian.mount_path` with vault-relative paths.
- Use `Path.relative_to` after `resolve()` to prove profile paths remain under the vault root.

- [ ] Run `uv run pytest tests/test_learning_paths.py -q`.
- [ ] Commit:

```bash
git add src/pynchy/config/models.py src/pynchy/config/settings.py src/pynchy/host/learning/__init__.py src/pynchy/host/learning/paths.py tests/test_learning_paths.py
git commit -m "feat: add learning config path resolver"
```

## Task 2: Vault Mount And Learned Skill Namespace

- [ ] Add failing mount and skill-sync tests.

Use `tests/test_container_runner.py` unless the file becomes hard to scan, then split focused tests into `tests/test_learning_skills.py`.

Test cases:

- Learning disabled does not add a vault mount.
- Learning enabled mounts the vault root read-write at `settings.learning.obsidian.mount_path`.
- Mount construction creates the profile fallback `memory` and `skills` directories.
- Learned skills under `<vault>/systems/pynchy/profiles/<profile>/skills/*/SKILL.md` are copied into the session `.claude/skills` directory when `skills = ["learned"]` or `skills = ["*"]`.
- Learned skills are not copied when workspace skills are `None`.
- A learned skill whose destination name collides with an already-copied built-in or plugin skill is skipped and logged.
- A learned skill symlink that escapes the `skills_root` is skipped.

Expected red failures:

```text
AssertionError: expected vault mount
TypeError: _sync_skills() got an unexpected keyword argument 'learned_skill_paths'
```

- [ ] Create `src/pynchy/host/learning/skills.py`.

Core API:

```python
def iter_learned_skill_dirs(group_folder: str) -> list[Path]:
    ...
```

Implementation notes:

- Call `resolve_learning_paths(group_folder)`.
- Return an empty list when learning is disabled or the skill root does not exist.
- Require each skill directory to have `SKILL.md`.
- Resolve each directory and verify it remains under `skills_root`.
- Enforce `settings.learning.skill_max_bytes` as a total directory byte budget per skill.
- Log skipped learned skills with the path and reason.

- [ ] Refactor skill copying in `src/pynchy/host/container_manager/session_prep.py`.

Keep existing behavior intact, then add learned skill copying after plugin skills:

```python
def _sync_skills(
    session_dir: Path,
    plugin_manager: pluggy.PluginManager | None = None,
    *,
    workspace_skills: list[str] | None = None,
    learned_skill_paths: list[Path] | None = None,
) -> None:
    ...
```

Implementation notes:

- Preserve `workspace_skills is None -> core only`.
- Learned skills should use the existing `parse_skill_tier` and `is_skill_selected` logic.
- Learned skill `SKILL.md` files should use Pynchy's existing skill format. Memory notes do not need a metadata contract.
- Copy only files in the skill directory, matching current skill behavior.
- If `skills_dst / skill_dir.name` already exists, skip the learned skill and log a warning.

- [ ] Modify `src/pynchy/host/container_manager/mounts.py`.

Implementation notes:

- Resolve learning paths during mount construction.
- Create `paths.memory_root` and `paths.skills_root`.
- Add `VolumeMount(str(paths.vault_root), paths.vault_mount_path, readonly=False)`.
- Pass `iter_learned_skill_dirs(group.folder)` into `_sync_skills`.
- Keep worktree, group, IPC, script, and env mounts unchanged.

- [ ] Run focused tests:

```bash
uv run pytest tests/test_container_runner.py::TestMountBuilding -q
uv run pytest tests/test_container_runner.py -k "skill or mount" -q
```

- [ ] Commit:

```bash
git add src/pynchy/host/learning/skills.py src/pynchy/host/container_manager/mounts.py src/pynchy/host/container_manager/session_prep.py tests/test_container_runner.py tests/test_learning_skills.py
git commit -m "feat: mount obsidian vault and sync learned skills"
```

If `tests/test_learning_skills.py` was not created, omit it from `git add`.

## Task 3: Durable Learning Queue

- [ ] Add failing tests in `tests/test_learning_queue.py`.

Test cases:

- `enqueue` writes a JSON job to `data/ipc/learning/pending`.
- `claim_next` atomically moves one pending job to `claimed` and increments attempts.
- A second queue instance cannot claim the same job after the first claim succeeds.
- `complete` moves the claimed job to `done`.
- `fail` requeues a claimed job until `max_attempts`, then moves it to `errors`.
- `requeue_expired` returns claimed jobs whose `lease_until` is in the past to `pending`.
- A claimed file with no `lease_until` is treated as expired.
- Invalid JSON moves to `errors` with a compact error note.

Expected red failures:

```text
ModuleNotFoundError: No module named 'pynchy.host.learning.queue'
```

- [ ] Create `src/pynchy/host/learning/queue.py`.

Core API:

```python
@dataclass(frozen=True)
class LearningPacket:
    job_id: str
    chat_jid: str
    group_folder: str
    profile: str
    created_at: str
    messages: list[dict[str, str]]
    final_answer: str | None
    tool_counts: dict[str, int]
    error_snippets: list[str]
    loaded_skills: list[str]
    provenance: dict[str, str]
    attempts: int = 0


@dataclass(frozen=True)
class ClaimedLearningPacket:
    packet: LearningPacket
    path: Path


class LearningQueue:
    def __init__(self, base_dir: Path | None = None, *, lease_seconds: int | None = None, max_attempts: int | None = None) -> None:
        ...

    def enqueue(self, packet: LearningPacket) -> Path:
        ...

    def claim_next(self, *, now: datetime | None = None) -> ClaimedLearningPacket | None:
        ...

    def complete(self, claimed: ClaimedLearningPacket) -> Path:
        ...

    def fail(self, claimed: ClaimedLearningPacket, reason: str) -> Path:
        ...

    def requeue_expired(self, *, now: datetime | None = None) -> int:
        ...
```

Implementation notes:

- Default base dir is `get_settings().data_dir / "ipc" / "learning"`.
- Directory names are `pending`, `claimed`, `done`, and `errors`.
- Use `write_json_atomic` for all writes.
- Use `Path.rename` for claim and state transitions.
- Claim by renaming from `pending` to `claimed`; catch `FileNotFoundError` and continue.
- Store `claimed_at`, `lease_until`, and `last_error` in the JSON payload when claiming or failing.
- Cap error strings before writing them to disk.

- [ ] Run `uv run pytest tests/test_learning_queue.py -q`.
- [ ] Commit:

```bash
git add src/pynchy/host/learning/queue.py tests/test_learning_queue.py
git commit -m "feat: add durable learning queue"
```

## Task 4: Capture Bounded Learning Packets After Successful Turns

- [ ] Add failing tests for packet construction in `tests/test_learning_reviewer.py` or a new `tests/test_learning_packets.py`.

Test cases:

- User messages are capped by `settings.learning.packet_max_chars`.
- Final assistant answer comes from `ContainerOutput(type="result", result=...)`.
- Tool counts increment from `ContainerOutput(type="tool_use", tool_name=...)`.
- Error snippets are capped and recorded from `status="error"`.
- Packet provenance includes chat JID, group folder, final cursor, and source message ids.

- [ ] Add failing pipeline tests in `tests/test_message_handler.py`.

Test cases:

- A clean successful turn enqueues exactly one learning packet after cursor advancement.
- A failure with no user-visible output does not enqueue because the batch will retry.
- A failure after partial user-visible output does not enqueue because the turn was not clean.
- Learning disabled does not enqueue.

Expected red failures:

```text
AssertionError: expected enqueue call
TypeError: _finalize_cursor_and_retry() got an unexpected keyword argument 'learning_summary'
```

- [ ] Create `src/pynchy/host/learning/packets.py`.

Core API:

```python
@dataclass
class LearningRunSummary:
    final_answer: str | None = None
    tool_counts: dict[str, int] = field(default_factory=dict)
    error_snippets: list[str] = field(default_factory=list)


def observe_container_output(summary: LearningRunSummary, output: ContainerOutput) -> None:
    ...


def build_learning_packet(
    *,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
    final_cursor: str,
    summary: LearningRunSummary,
) -> LearningPacket | None:
    ...


def enqueue_learning_packet(... ) -> Path | None:
    ...
```

Implementation notes:

- Return `None` when learning is disabled or paths cannot resolve.
- Use `profile_name_for_group(group.folder)` for profile selection.
- Include only user-visible new messages from the current batch.
- Cap by characters, not tokens.
- Do not include raw tool inputs unless a compact allowlisted summary already exists. For v1, tool name counts are enough.
- Do not enqueue from reset handoff logic.

- [ ] Modify `src/pynchy/host/orchestrator/messaging/pipeline.py`.

Implementation notes:

- Instantiate `learning_summary = LearningRunSummary()` in `process_group_messages`.
- In `on_output`, call `observe_container_output(learning_summary, result)` before or after `handle_streamed_output`.
- Pass `learning_summary` into `_finalize_cursor_and_retry`.
- Inside `_finalize_cursor_and_retry`, after `advance_cursor` and before `background_merge_worktree(group)`, call `enqueue_learning_packet(...)` only when `failed` is false.
- If enqueue fails, log the exception and keep the user turn successful.

- [ ] Run focused tests:

```bash
uv run pytest tests/test_message_handler.py -k "learning or finalize or retry" -q
uv run pytest tests/test_learning_reviewer.py tests/test_learning_packets.py -q
```

If `tests/test_learning_packets.py` was not created, omit it from the command.

- [ ] Commit:

```bash
git add src/pynchy/host/learning/packets.py src/pynchy/host/orchestrator/messaging/pipeline.py tests/test_message_handler.py tests/test_learning_reviewer.py tests/test_learning_packets.py
git commit -m "feat: enqueue learning packets after turns"
```

If `tests/test_learning_packets.py` was not created, omit it from `git add`.

## Task 5: Reviewer Prompt And Learning Worker

- [ ] Add failing reviewer prompt tests in `tests/test_learning_reviewer.py`.

Test cases:

- The prompt names the vault mount path as the global memory namespace.
- The prompt tells the reviewer to classify memory by existing folders and use the profile fallback only when no stronger semantic folder is evident.
- The prompt identifies profile-scoped fallback memory and skill paths.
- The prompt says memory notes are folder-governed and should not depend on semantic frontmatter.
- The prompt says learned skills belong under the profile skill namespace and should use Pynchy's existing `SKILL.md` skill format.
- The deterministic prefilter skips short acknowledgements and casual turns with no tools, no errors, and no explicit learning signal.
- The deterministic prefilter accepts turns with explicit phrases such as "remember", "learn this", "save this", tool error recovery, or skill-worthy repeated workflow.

- [ ] Add failing worker tests in `tests/test_learning_worker.py`.

Test cases:

- Worker requeues expired jobs before claiming a new job.
- Worker completes skipped packets without running the agent.
- Worker runs the hidden reviewer for accepted packets.
- Worker uses a synthetic `WorkspaceProfile` with a synthetic chat JID.
- Worker calls `run_agent(..., is_scheduled_task=True, input_source="user")` so the prompt is trace-only and the synthetic session is isolated.
- Worker marks success as done.
- Worker marks reviewer failure through `queue.fail`.

Expected red failures:

```text
ModuleNotFoundError: No module named 'pynchy.host.learning.worker'
```

- [ ] Create `src/pynchy/host/learning/reviewer.py`.

Core API:

```python
def should_review(packet: LearningPacket) -> bool:
    ...


def build_review_prompt(packet: LearningPacket, paths: LearningPaths) -> str:
    ...
```

Prompt requirements:

- "The mounted vault root is the global memory namespace."
- "Use existing folder organization first."
- "Use the profile fallback memory path only when no repo, machine, subject, or other existing folder clearly fits."
- "Write learned skills only under the profile skill path."
- "Do not invent semantic frontmatter requirements for memory notes."
- "Keep notes small and factual; update existing notes when that is cleaner than adding new ones."
- "If nothing durable was learned, make no filesystem changes."

- [ ] Create `src/pynchy/host/learning/worker.py`.

Core API:

```python
@dataclass(frozen=True)
class LearningWorkerDeps:
    run_agent: Callable[..., Awaitable[str]]
    queue: LearningQueue


async def process_one_learning_job(deps: LearningWorkerDeps) -> bool:
    ...


async def start_learning_worker_loop(deps: LearningWorkerDeps) -> None:
    ...
```

Implementation notes:

- Use `resolve_learning_paths(packet.group_folder, profile_override=packet.profile)` for prompt paths.
- The reviewer can run in a synthetic group such as `learning-review-{profile_slug}` because the vault root mount is global and the prompt carries the source profile paths.
- Use `WorkspaceProfile(jid=f"learning-review:{profile_slug}", name="Learning Reviewer", folder=f"learning-review-{profile_slug}", trigger="", is_admin=False)`.
- Call `run_agent` with `is_scheduled_task=True` and `input_source="user"`.
- Capture reviewer output with an `on_output` callback but do not route it through `handle_streamed_output`.
- Treat `run_agent` returning `"success"` as done; anything else is retryable failure.
- Sleep for `settings.learning.queue_poll_interval_seconds` between empty polls.
- Let cancellation propagate cleanly.

- [ ] Run focused tests:

```bash
uv run pytest tests/test_learning_reviewer.py tests/test_learning_worker.py -q
```

- [ ] Commit:

```bash
git add src/pynchy/host/learning/reviewer.py src/pynchy/host/learning/worker.py tests/test_learning_reviewer.py tests/test_learning_worker.py
git commit -m "feat: add learning reviewer worker"
```

## Task 6: Lifecycle Wiring

- [ ] Add failing lifecycle or dependency-factory tests.

Test cases:

- `make_learning_deps(app)` returns a `LearningWorkerDeps` using `app.run_agent` and a `LearningQueue`.
- `_start_subsystems` starts the learning worker only when `settings.learning.enabled` and `settings.learning.review_after_turn` are both true.
- `_start_subsystems` does not start the worker when learning is disabled.

Expected red failures:

```text
ImportError: cannot import name 'make_learning_deps'
AssertionError: expected learning-worker task
```

- [ ] Modify `src/pynchy/host/orchestrator/dep_factory.py`.

Implementation note:

```python
def make_learning_deps(app: PynchyApp) -> LearningWorkerDeps:
    return LearningWorkerDeps(run_agent=app.run_agent, queue=LearningQueue())
```

- [ ] Modify `src/pynchy/host/orchestrator/lifecycle.py`.

Implementation notes:

- Import `make_learning_deps` and `start_learning_worker_loop`.
- Append a background task named `learning-worker` only when enabled and review-after-turn is true.
- Keep scheduler, IPC watcher, git sync, tunnels, and HTTP startup ordering stable.

- [ ] Run focused tests:

```bash
uv run pytest tests/test_orchestrator_lifecycle.py tests/test_learning_worker.py -q
```

If `tests/test_orchestrator_lifecycle.py` does not exist, add or use the current lifecycle test file found with `rg -n "_start_subsystems|create_background_task" tests`.

- [ ] Commit:

```bash
git add src/pynchy/host/orchestrator/dep_factory.py src/pynchy/host/orchestrator/lifecycle.py tests/test_orchestrator_lifecycle.py
git commit -m "feat: start learning worker from lifecycle"
```

If `tests/test_orchestrator_lifecycle.py` was not created, add the actual lifecycle test file instead.

## Task 7: Config Example And Documentation

- [ ] Update `config-examples/config.toml.EXAMPLE`.

Example section:

```toml
[learning]
enabled = false
review_after_turn = true
queue_poll_interval_seconds = 5.0
lease_seconds = 300
max_attempts = 3
packet_max_chars = 12000
skill_max_bytes = 200000

[learning.obsidian]
vault_root = "/path/to/obsidian/vault"
mount_path = "/workspace/vault"
default_profile_root = "systems/pynchy/profiles/{profile}"
memory_dir_name = "memory"
skills_dir_name = "skills"
```

- [ ] Update docs.

Document:

- Enabling learning mounts the vault root read-write at `/workspace/vault` by default.
- The vault root is the global memory namespace.
- Agents should choose existing semantic folders first.
- Profile fallback path is `systems/pynchy/profiles/{profile}/memory`.
- Learned skills live under `systems/pynchy/profiles/{profile}/skills`.
- Skill activation still follows existing workspace `skills` selection. To include learned skills for a profile/workspace, configure `skills = ["learned"]` or `skills = ["*"]`.
- Queue files live under `data/ipc/learning` and survive restarts.
- Access controls and narrower mounts are outside v1.

- [ ] Run docs validation:

```bash
uv run mkdocs build --strict
```

- [ ] Commit:

```bash
git add config-examples/config.toml.EXAMPLE docs mkdocs.yml
git commit -m "docs: document obsidian learning"
```

Only include `mkdocs.yml` if it changed.

## Task 8: End-To-End Verification

- [ ] Run targeted tests:

```bash
uv run pytest tests/test_learning_paths.py tests/test_learning_queue.py tests/test_learning_reviewer.py tests/test_learning_worker.py -q
uv run pytest tests/test_container_runner.py -k "skill or mount" -q
uv run pytest tests/test_message_handler.py -k "learning or finalize or retry" -q
```

- [ ] Run broader relevant tests:

```bash
uv run pytest tests/test_container_runner.py tests/test_message_handler.py -q
```

- [ ] Run docs:

```bash
uv run mkdocs build --strict
```

- [ ] Run lint/type gate if the targeted suite is green:

```bash
uvx ruff check src/pynchy/host/learning src/pynchy/config src/pynchy/host/container_manager src/pynchy/host/orchestrator tests
uv run mypy src/pynchy/host/learning src/pynchy/config src/pynchy/host/container_manager src/pynchy/host/orchestrator
```

- [ ] Manual smoke test with a temporary vault:

```bash
mkdir -p /tmp/pynchy-learning-vault
```

Configure learning against that vault in a local config, then send a learning-worthy message through a dev group. Verify:

- The group container has `/workspace/vault`.
- `data/ipc/learning/pending` receives a job after a clean turn.
- The worker claims the job.
- The job reaches `done` or `errors`.
- If the reviewer writes a note, it lands under the temp vault in either an existing semantic folder or `systems/pynchy/profiles/<profile>/memory`.
- A learned skill under `systems/pynchy/profiles/<profile>/skills/<skill>/SKILL.md` is copied into a later session only when that workspace selects the `learned` tier.

- [ ] Commit any final fixes:

```bash
git add src tests docs config-examples
git commit -m "test: verify obsidian learning integration"
```

Create this commit only if verification required changes.

## Risks And Guardrails

- Do not feed whole transcripts to the reviewer. The packet cap is part of the cost control design.
- Do not enqueue on retryable failures. Failed batches can be retried by the normal queue and should not become memory.
- Do not let learned skills overwrite built-in or plugin skills.
- Do not treat workspace folder names as the namespace for memory. Profiles are the sharing boundary.
- Do not put access-control policy into v1 path resolution. The v1 security model is explicit broad vault mounting by config.
- Do not require memory-note frontmatter. Folder location is the contract.

## Final Review Checklist

- [ ] Every new config key has a default and a docs entry.
- [ ] Learned skills require explicit skill selection through existing `skills` config.
- [ ] The learning worker does not broadcast prompts or outputs to user channels.
- [ ] Restart during review leaves the job claimable after lease expiry.
- [ ] The vault mount is read-write only when learning is enabled.
- [ ] Tests prove clean success enqueues and both failure paths do not.
- [ ] Docs state that the vault root is the global memory namespace.
