# Pynchy Config Schema Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current noisy config schema with a smaller capability-oriented schema: composable profiles, workspace profile lists, tools instead of services/MCP, prompts instead of directives, repo lists, deterministic repo mounts, and isolated jobs.

**Architecture:** Treat TOML as a strict operator-facing language, not a compatibility surface. Parse directly into new Pydantic models, reject legacy keys loudly, and update downstream code to consume the new vocabulary. Keep runtime state such as platform chat/thread targets out of static config.

**Tech Stack:** Python, Pydantic settings, tomllib/tomlkit, pytest, ruff, existing Pynchy container/worktree/MCP/orchestrator modules.

---

## Target Schema

```toml
[agent]
name = "pynchy"
trigger_aliases = ["ghost"]
default_core = "codex"

[repos]
root = "/Users/ricardo/src/PERSONAL"

[profiles.base]
prompts = ["base", "idle-escape"]
tools = ["task_tracking"]

[profiles.pynchy-dev]
includes = ["base"]
prompts = ["pynchy-admin-ops", "pynchy-code-improver"]
skills = ["core", "ops"]
repo = "crypdick/pynchy"
tools = ["linear"]
is_admin = true

[profiles.project-managing]
includes = ["base"]
skills = ["calendar-caldav", "project-managing", "gcal", "dddd"]
tools = ["calendar"]
repo = "get-synapse-ai/gantt-believe-it"

[workspaces.pynchy-dev]
profiles = ["pynchy-dev"]

[workspaces.dddd-evening-review]
profiles = ["project-managing"]

[tools.browser]
type = "mcp"
public_source = true
secret_data = false
public_sink = true
dangerous_writes = true

[tools.browser.mcp]
runtime = "docker"
image = "pynchy-mcp-browser:latest"
port = 3000
transport = "streamable_http"

[tools.task_tracking]
type = "linear"
enabled = true
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false
api_key_env = "LINEAR_API_KEY" # pragma: allowlist secret - env var name, not a secret value
project_per_workspace = true
project_name_template = "Pynchy: {workspace}"

[connections.synapse]
type = "discord"
default = true
bot_token_env = "DISCORD_BOT_TOKEN" # pragma: allowlist secret - env var name, not a secret value
dm_policy = "allowlist"
allow_from = ["crypdick"]
group_policy = "allowlist"
```

Legacy keys such as `directives`, `universal`, `sandbox_*`, `services`, `mcp`, `mcp_servers`, `chat`, `context_mode`, `idle_terminate`, `access`, `mode`, `trust`, `trigger`, `allowed_users`, `fallback_model`, and `[owner]` must fail validation.

## File Structure

- Modify `src/pynchy/config/models.py`: new strict models for agents, repos, profiles, workspaces, discriminated tools, and connections.
- Modify `src/pynchy/config/settings.py`: root fields, old-key rejection, profile/tool/default-connection validation.
- Modify `src/pynchy/config/merge.py`: composable profile merge, prompts/tools/repo vocabulary, no workspace cascade fields.
- Rename `src/pynchy/config/directives.py` to `src/pynchy/config/prompts.py`.
- Rename `directives/` to `prompts/`.
- Modify orchestrator modules under `src/pynchy/host/orchestrator/`: consume resolved profile config and runtime-created conversation targets.
- Modify container manager modules under `src/pynchy/host/container_manager/`: always idle terminate, mount repos under `/workspace/repos/<owner>/<repo>`, resolve MCP tools.
- Modify git modules under `src/pynchy/host/git_ops/`: derive repo roots from `[repos].root` with optional per-repo overrides.
- Modify tests under `tests/`: add schema cutover tests and update existing config/workspace/container/git assertions.
- Modify docs and examples: replace old config vocabulary.

---

### Task 1: Add Config Schema Acceptance And Rejection Tests

**Files:**
- Create: `tests/test_config_schema_cutover.py`

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.toml_io import parse_settings_toml


MINIMAL_CONFIG = """
[agent]
name = "pynchy"
trigger_aliases = ["ghost"]
default_core = "codex"

[repos]
root = "/Users/ricardo/src/PERSONAL"

[profiles.base]
prompts = ["base", "idle-escape"]
tools = ["task_tracking"]

[profiles.dev]
includes = ["base"]
skills = ["core"]
repo = ["crypdick/pynchy", "get-synapse-ai/gantt-believe-it"]
is_admin = true

[workspaces.dev]
profiles = ["dev"]

[tools.task_tracking]
type = "linear"
enabled = true
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false
api_key_env = "LINEAR_API_KEY" # pragma: allowlist secret - env var name, not a secret value
project_per_workspace = true
project_name_template = "Pynchy: {workspace}"

[connections.synapse]
type = "discord"
default = true
bot_token_env = "DISCORD_BOT_TOKEN" # pragma: allowlist secret - env var name, not a secret value
dm_policy = "allowlist"
allow_from = ["crypdick"]
group_policy = "allowlist"
"""


def test_new_schema_parses_minimal_config():
    settings = parse_settings_toml(MINIMAL_CONFIG)
    resolved = settings.resolved_workspace_config("dev")

    assert settings.agent.default_core == "codex"
    assert settings.repos.root == "/Users/ricardo/src/PERSONAL"
    assert settings.workspaces["dev"].profiles == ["dev"]
    assert resolved is not None
    assert resolved.prompts == ["base", "idle-escape"]
    assert resolved.skills == ["core"]
    assert resolved.tools == ["task_tracking"]
    assert resolved.repo == ["crypdick/pynchy", "get-synapse-ai/gantt-believe-it"]
    assert resolved.is_admin is True


@pytest.mark.parametrize(
    "legacy",
    [
        "[universal]\\ndirectives = ['base']\\n",
        "[profiles.base]\\ndirectives = ['base']\\n",
        "[workspaces.dev]\\nchat = 'connection.discord.synapse.chat.general'\\n",
        "[workspaces.dev]\\ncontext_mode = 'isolated'\\n",
        "[workspaces.dev]\\nidle_terminate = true\\n",
        "[workspaces.dev]\\naccess = 'readwrite'\\n",
        "[workspaces.dev]\\nmode = 'agent'\\n",
        "[workspaces.dev]\\ntrust = true\\n",
        "[workspaces.dev]\\ntrigger = 'mention'\\n",
        "[workspaces.dev]\\nallowed_users = ['*']\\n",
        "[profiles.dev]\\nfallback_model = 'gpt-5-mini'\\n",
        "[owner]\\nslack = 'Ricardo'\\n",
        "[services.browser]\\npublic_source = true\\n",
        "[mcp.browser]\\ntype = 'docker'\\n",
    ],
)
def test_legacy_schema_keys_are_rejected(legacy: str):
    with pytest.raises(ValidationError):
        parse_settings_toml(MINIMAL_CONFIG + "\n" + legacy)


def test_workspace_can_only_select_profiles():
    text = MINIMAL_CONFIG + "\n[workspaces.general]\nprofiles = ['base']\nskills = ['extra']\n"
    with pytest.raises(ValidationError, match="skills"):
        parse_settings_toml(text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_schema_cutover.py -q`

Expected: fails because the new schema fields are not implemented yet.

- [ ] **Step 3: Commit tests**

```bash
git add tests/test_config_schema_cutover.py
git commit -m "test: define new config schema cutover"
```

---

### Task 2: Replace Core Config Models

**Files:**
- Modify: `src/pynchy/config/models.py`
- Modify: `src/pynchy/config/settings.py`
- Test: `tests/test_config_schema_cutover.py`

- [ ] **Step 1: Update config models**

In `src/pynchy/config/models.py`, replace affected model shapes with:

```python
class AgentConfig(_StrictModel):
    name: str = "pynchy"
    trigger_aliases: list[str] = ["ghost"]
    default_core: str = "openai"
    model: str | None = "gpt-5.5"


class ReposConfig(_StrictModel):
    root: str = "/Users/ricardo/src/PERSONAL"
    overrides: dict[str, RepoConfig] = {}


class ProfileConfig(_StrictModel):
    includes: list[str] = []
    prompts: list[str] = []
    skills: list[str] = []
    tools: list[str] = []
    repo: str | list[str] | None = None
    model: str | None = None
    is_admin: bool = False
    contains_secrets: bool = False


class WorkspaceConfig(_StrictModel):
    profiles: list[str]


class McpToolConfig(_StrictModel):
    runtime: str = "docker"
    image: str | None = None
    port: int | None = None
    transport: Literal["sse", "streamable_http", "stdio"] = "streamable_http"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    env_forward: dict[str, str] = {}
    volumes: list[str] = []
    idle_timeout: int | None = None
    inject_workspace: bool = True
    credentials_path: str | None = None


class McpTool(_StrictModel):
    type: Literal["mcp"]
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True
    mcp: McpToolConfig


class LinearTool(_StrictModel):
    type: Literal["linear"]
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True
    api_key_env: str | None = None
    project_per_workspace: bool | None = None
    project_name_template: str | None = None


class CaldavTool(_StrictModel):
    type: Literal["caldav"]
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True
    config: dict[str, Any] = {}


class BuiltinTool(_StrictModel):
    type: Literal["builtin"]
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True


ToolConfig = Annotated[
    McpTool | LinearTool | CaldavTool | BuiltinTool,
    Field(discriminator="type"),
]
```

- [ ] **Step 2: Add semantic config value types**

Add zero-cost semantic types for domain strings that cross module boundaries:

```python
ProfileName = NewType("ProfileName", str)
ToolName = NewType("ToolName", str)
WorkspaceName = NewType("WorkspaceName", str)
RepoSlug = NewType("RepoSlug", str)
```

Use `Annotated[..., AfterValidator(...)]` wrappers where the TOML boundary proves a format, such as a repo slug shaped as `owner/repo`. Do not pass raw `str` through downstream config, worktree, or container APIs when the value represents one of these domain concepts.

- [ ] **Step 3: Update root settings fields**

In `src/pynchy/config/settings.py`, use:

```python
repos: ReposConfig = ReposConfig()
profiles: dict[ProfileName, ProfileConfig] = {}
workspaces: dict[WorkspaceName, WorkspaceConfig] = Field(default_factory=dict)
tools: dict[ToolName, ToolConfig] = {}
connections: dict[str, ConnectionConfig] = {}
```

Remove root fields for `universal`, `services`, `mcp_servers`, `mcp_groups`, `mcp_presets`, `mcp_server_instances`, `connection`, `caldav`, and `owner`.

- [ ] **Step 4: Reject old keys explicitly**

Update `_reject_legacy_sections` so deleted root keys produce one specific validation error:

```python
legacy_root_keys = {
    "universal",
    "sandbox",
    "sandbox_universal",
    "sandbox_profiles",
    "services",
    "mcp",
    "mcp_servers",
    "mcp_groups",
    "mcp_presets",
    "connection",
    "owner",
    "caldav",
    "channels",
    "slack",
    "workspace_defaults",
    "directives",
    "cron_jobs",
}
```

- [ ] **Step 5: Run the schema tests**

Run: `uv run pytest tests/test_config_schema_cutover.py -q`

Expected: model-level failures improve; merge and downstream validation can still fail until later tasks.

---

### Task 3: Implement Composable Profile Resolution

**Files:**
- Modify: `src/pynchy/config/merge.py`
- Modify: `src/pynchy/config/settings.py`
- Test: `tests/test_config_schema_cutover.py`
- Test: `tests/test_merge.py`

- [ ] **Step 1: Add profile composition tests**

Add:

```python
def test_profiles_compose_in_order_with_union_fields_and_last_scalar_wins():
    text = MINIMAL_CONFIG + """
[profiles.admin-extra]
includes = ["base"]
prompts = ["admin"]
tools = ["browser"]
model = "gpt-5.5"

[profiles.workspace-final]
includes = ["admin-extra"]
prompts = ["workspace"]
skills = ["ops"]
repo = "crypdick/pynchy"
model = "gpt-5.5-high"

[workspaces.composed]
profiles = ["workspace-final"]

[tools.browser]
type = "mcp"
public_source = true
secret_data = false
public_sink = true
dangerous_writes = true
"""
    settings = parse_settings_toml(text)
    resolved = settings.resolved_workspace_config("composed")
    assert resolved is not None
    assert resolved.prompts == ["base", "idle-escape", "admin", "workspace"]
    assert resolved.tools == ["task_tracking", "browser"]
    assert resolved.skills == ["ops"]
    assert resolved.repo == ["crypdick/pynchy"]
    assert resolved.model == "gpt-5.5-high"


def test_profile_cycles_are_rejected():
    text = MINIMAL_CONFIG + """
[profiles.a]
includes = ["b"]

[profiles.b]
includes = ["a"]

[workspaces.cycle]
profiles = ["a"]
"""
    with pytest.raises(ValidationError, match="profile cycle"):
        parse_settings_toml(text)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_config_schema_cutover.py::test_profiles_compose_in_order_with_union_fields_and_last_scalar_wins tests/test_config_schema_cutover.py::test_profile_cycles_are_rejected -q`

Expected: fails because only the old three-tier merge exists.

- [ ] **Step 3: Replace merge implementation**

Define:

```python
@dataclass(frozen=True)
class ResolvedWorkspaceConfig:
    prompts: list[str]
    skills: list[str]
    tools: list[str]
    repo: list[str]
    model: str | None
    is_admin: bool
    contains_secrets: bool
```

Implement depth-first `includes` expansion. Union fields: `prompts`, `skills`, `tools`, `repo`. Scalar field: `model`, later profiles win. Boolean fields: `is_admin`, `contains_secrets`, OR together.

- [ ] **Step 4: Update `Settings.resolved_workspace_config`**

Use:

```python
def resolved_workspace_config(self, workspace_name: str) -> ResolvedWorkspaceConfig | None:
    workspace = self.workspaces.get(workspace_name)
    if workspace is None:
        return None
    return resolve_profiles(self.profiles, workspace.profiles)
```

- [ ] **Step 5: Run merge tests**

Run: `uv run pytest tests/test_config_schema_cutover.py tests/test_merge.py -q`

Expected: PASS after old merge tests are updated to prompts/tools/repo semantics.

---

### Task 4: Rename Directives To Prompts Everywhere

**Files:**
- Rename: `directives/` to `prompts/`
- Rename: `src/pynchy/config/directives.py` to `src/pynchy/config/prompts.py`
- Rename: `tests/test_directives.py` to `tests/test_prompts.py`
- Modify imports across `src/pynchy/`, `tests/`, and docs.

- [ ] **Step 1: Rename prompt loader test**

Update imports:

```python
from pynchy.config.prompts import read_prompts
```

Add or preserve:

```python
def test_read_prompts_concatenates_named_prompt_files(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "base.md").write_text("Base prompt")
    (prompts_dir / "ops.md").write_text("Ops prompt")
    monkeypatch.chdir(tmp_path)

    assert read_prompts(["base", "ops"]) == "Base prompt\n\nOps prompt"
```

- [ ] **Step 2: Run prompt tests to verify failure**

Run: `uv run pytest tests/test_prompts.py -q`

Expected: fails until files and symbols are renamed.

- [ ] **Step 3: Rename files with git**

Run:

```bash
git mv directives prompts
git mv src/pynchy/config/directives.py src/pynchy/config/prompts.py
git mv tests/test_directives.py tests/test_prompts.py
```

- [ ] **Step 4: Update symbols**

Use `rg -n "directive|directives|read_directives|directives/" src tests docs` and replace `directives` vocabulary with `prompts`. Do not leave compatibility names.

- [ ] **Step 5: Run prompt tests**

Run: `uv run pytest tests/test_prompts.py -q`

Expected: PASS.

---

### Task 5: Replace Services And MCP Config With Tools

**Files:**
- Modify: `src/pynchy/config/settings.py`
- Modify: `src/pynchy/config/mcp.py` or fold it into tool models.
- Modify: `src/pynchy/host/container_manager/mcp/`
- Modify: `src/pynchy/host/container_manager/security/`
- Test: `tests/test_config_trust.py`
- Test: `tests/test_mcp_port_allocation.py`
- Test: `tests/test_trust_config.py`

- [ ] **Step 1: Write failing tool trust tests**

```python
def test_tool_trust_defaults_are_maximally_cautious():
    cfg = BuiltinTool(type="builtin")
    assert cfg.public_source is True
    assert cfg.secret_data is True
    assert cfg.public_sink is True
    assert cfg.dangerous_writes is True


def test_mcp_tool_config_folds_credentials_path_into_provider_block():
    adapter = TypeAdapter(ToolConfig)
    cfg = adapter.validate_python(
        {
            "type": "mcp",
            "public_source": False,
            "secret_data": True,
            "public_sink": False,
            "dangerous_writes": False,
            "mcp": {"credentials_path": "/gdrive-server/credentials.json"},
        }
    )
    assert isinstance(cfg, McpTool)
    assert cfg.mcp.credentials_path == "/gdrive-server/credentials.json"


def test_mcp_tool_requires_mcp_provider_config():
    adapter = TypeAdapter(ToolConfig)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "type": "mcp",
                "public_source": False,
                "secret_data": True,
                "public_sink": False,
                "dangerous_writes": False,
            }
        )
```

Do not implement this shape:

```python
class ToolConfig(_StrictModel):
    type: Literal["mcp", "linear", "caldav", "builtin"]
    mcp: McpToolConfig | None = None
```

That optional-field blob makes illegal states representable and violates `CONVENTIONS.md`.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_config_trust.py -q`

Expected: fails because `ToolConfig`, `BuiltinTool`, and `McpTool` do not exist.

- [ ] **Step 3: Implement tool-backed MCP resolution**

Add:

```python
def mcp_tools_for_names(self, names: Iterable[str]) -> dict[str, McpToolConfig]:
    result = {}
    for name in names:
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(tool, McpTool):
            continue
        result[name] = tool.mcp
    return result
```

This is a new tool-to-MCP adapter, not a legacy alias.

- [ ] **Step 4: Update security lookup**

Replace service trust lookup with tool trust lookup:

```python
tool = settings.tools.get(tool_name)
public_source = tool.public_source if tool else True
```

- [ ] **Step 5: Run tool and MCP tests**

Run: `uv run pytest tests/test_config_trust.py tests/test_trust_config.py tests/test_mcp_port_allocation.py -q`

Expected: PASS after fixtures move from `mcp_servers`/`services` to `tools`.

---

### Task 6: Cut Workspace Config Down To Profiles Only

**Files:**
- Modify: `src/pynchy/host/orchestrator/workspace_config.py`
- Modify: `src/pynchy/host/orchestrator/workspace_registration.py`
- Modify: tests constructing `WorkspaceConfig`.

- [ ] **Step 1: Write failing workspace purity test**

```python
def test_workspace_config_only_accepts_profiles():
    cfg = WorkspaceConfig(profiles=["base"])
    assert cfg.profiles == ["base"]
    with pytest.raises(ValidationError):
        WorkspaceConfig(profiles=["base"], repo="owner/repo")
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_workspace_config.py::test_workspace_config_only_accepts_profiles -q`

Expected: fails until `WorkspaceConfig` is reduced.

- [ ] **Step 3: Update workspace reconciliation**

Replace direct field reads from `WorkspaceConfig` with:

```python
resolved = settings.resolved_workspace_config(folder)
```

Use `resolved.prompts`, `resolved.skills`, `resolved.tools`, `resolved.repo`, `resolved.model`, `resolved.is_admin`, and `resolved.contains_secrets`.

- [ ] **Step 4: Remove static chat validation**

Delete validation requiring `workspaces.<name>.chat`. Platform target creation/reuse belongs in runtime state keyed by workspace name.

- [ ] **Step 5: Run workspace tests**

Run: `uv run pytest tests/test_workspace_config.py tests/test_workspace_reconcile.py tests/test_dynamic_thread_workspaces.py -q`

Expected: PASS after updating tests to profile-selected workspace behavior.

---

### Task 7: Rework Repo Resolution And Mounts

**Files:**
- Modify: `src/pynchy/host/git_ops/repo.py`
- Modify: `src/pynchy/host/git_ops/worktree.py`
- Modify: `src/pynchy/host/container_manager/mounts.py`
- Modify: `src/pynchy/agent/agent_runner/src/agent_runner/main.py`
- Test: `tests/test_repo_tokens.py`
- Test: `tests/test_worktree.py`
- Test: `tests/test_container_runner.py`
- Test: `tests/test_agent_runner.py`

- [ ] **Step 1: Write failing repo mount tests**

```python
def test_single_repo_mount_uses_workspace_repos_layout(tmp_path):
    mounts = build_repo_mounts(["crypdick/pynchy"], tmp_path)
    assert mounts[0].container_path == "/workspace/repos/crypdick/pynchy"


def test_multiple_repo_mounts_use_workspace_repos_layout(tmp_path):
    mounts = build_repo_mounts(
        ["crypdick/pynchy", "get-synapse-ai/gantt-believe-it"],
        tmp_path,
    )
    assert [m.container_path for m in mounts] == [
        "/workspace/repos/crypdick/pynchy",
        "/workspace/repos/get-synapse-ai/gantt-believe-it",
    ]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_container_runner.py::test_single_repo_mount_uses_workspace_repos_layout tests/test_container_runner.py::test_multiple_repo_mounts_use_workspace_repos_layout -q`

Expected: fails because current code uses `/workspace/project`.

- [ ] **Step 3: Update repo context resolution**

Derive roots as:

```python
root = Path(settings.repos.root) / owner / repo_name
```

unless `settings.repos.overrides[slug].path` exists. Worktrees stay under `settings.worktrees_dir / owner / repo_name`.

- [ ] **Step 4: Update container input**

Replace `repo_access: str | None` with:

```python
repos: list[str]
```

Serialize no field when empty.

- [ ] **Step 5: Update mount layout**

Always mount repo worktrees under `/workspace/repos/<owner>/<repo>`. Do not create `/workspace/project`.

- [ ] **Step 6: Update agent default cwd**

For repo-backed workspaces, default cwd is `/workspace/repos/<first-owner>/<first-repo>`. For no-repo workspaces, keep a neutral workspace directory.

- [ ] **Step 7: Run repo/container tests**

Run: `uv run pytest tests/test_repo_tokens.py tests/test_worktree.py tests/test_container_runner.py tests/test_agent_runner.py -q`

Expected: PASS after updating assertions from `repo_access` to `repos`.

---

### Task 8: Make Jobs Always Isolated And Containers Always Idle-Terminate

**Files:**
- Modify: `src/pynchy/config/jobs.py`
- Modify: `src/pynchy/host/orchestrator/task_scheduler.py`
- Modify: `src/pynchy/host/orchestrator/config_jobs.py`
- Modify: `src/pynchy/host/container_manager/orchestrator.py`
- Test: `tests/test_temporal_scheduler.py`
- Test: `tests/test_task_scheduler.py`
- Test: `tests/test_container_runner.py`

- [ ] **Step 1: Write failing tests**

```python
def test_job_config_rejects_context_mode():
    with pytest.raises(ValidationError):
        JobConfig(
            schedule="0 5 * * *",
            workspace="dev",
            prompt="Run review",
            context_mode="group",
        )
```

```python
def test_container_idle_termination_is_not_workspace_configurable():
    resolved = ResolvedWorkspaceConfig(
        prompts=[],
        skills=[],
        tools=[],
        repo=[],
        model=None,
        is_admin=False,
        contains_secrets=False,
    )
    assert "idle_terminate" not in resolved.__dataclass_fields__
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_task_scheduler.py::test_job_config_rejects_context_mode tests/test_container_runner.py::test_container_idle_termination_is_not_workspace_configurable -q`

Expected: fails until `context_mode` and `idle_terminate` are removed.

- [ ] **Step 3: Remove config fields**

Delete `context_mode` from `JobConfig`, profile resolution, scheduled task serialization, and scheduler code. Delete `idle_terminate` from profile/workspace models and resolved config.

- [ ] **Step 4: Enforce job isolation in runtime code**

When launching a job, always create or reuse the job-specific runtime thread/session. Do not read isolation behavior from config.

- [ ] **Step 5: Enforce idle termination in runtime code**

Container lifecycle always uses the configured container idle timeout. No workspace/profile switch may disable it.

- [ ] **Step 6: Run scheduler and container tests**

Run: `uv run pytest tests/test_temporal_scheduler.py tests/test_task_scheduler.py tests/test_container_runner.py -q`

Expected: PASS.

---

### Task 9: Update Connections To New Namespace And Default Selection

**Files:**
- Modify: `src/pynchy/config/models.py`
- Modify: `src/pynchy/config/settings.py`
- Modify: channel plugin config readers.
- Test: `tests/test_discord_config.py`

- [ ] **Step 1: Write failing connection tests**

```python
def test_connections_namespace_accepts_default_discord_connection():
    settings = parse_settings_toml(MINIMAL_CONFIG)
    assert settings.default_connection_name == "synapse"
    assert settings.connections["synapse"].type == "discord"


def test_only_one_connection_can_be_default():
    text = MINIMAL_CONFIG + """
[connections.other]
type = "discord"
default = true
bot_token_env = "OTHER_DISCORD_TOKEN" # pragma: allowlist secret - env var name, not a secret value
"""
    with pytest.raises(ValidationError, match="default connection"):
        parse_settings_toml(text)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_discord_config.py -q`

Expected: fails because `[connection.discord.*]` is still expected.

- [ ] **Step 3: Implement discriminated connection models**

Use a discriminated union so platform-specific required fields get parsed at the boundary:

```python
class DiscordConnection(_StrictModel):
    type: Literal["discord"]
    default: bool = False
    bot_token_env: str
    dm_policy: Literal["open", "allowlist", "disabled"] | None = None
    allow_from: list[str] = []
    group_policy: Literal["open", "disabled", "allowlist"] | None = None


class SlackConnection(_StrictModel):
    type: Literal["slack"]
    default: bool = False
    bot_token_env: str
    app_token_env: str


class WhatsAppConnection(_StrictModel):
    type: Literal["whatsapp"]
    default: bool = False
    auth_db_path: str | None = None


ConnectionConfig = Annotated[
    DiscordConnection | SlackConnection | WhatsAppConnection,
    Field(discriminator="type"),
]
```

Do not use a single model with optional `bot_token_env`, `app_token_env`, and platform policy fields. That shape makes invalid cross-platform combinations representable.

- [ ] **Step 4: Validate default connection**

Require exactly one default connection when workspaces exist:

```python
defaults = [name for name, conn in self.connections.items() if conn.default]
if self.workspaces and len(defaults) != 1:
    raise ValueError("exactly one default connection is required when workspaces are configured")
```

- [ ] **Step 5: Run connection tests**

Run: `uv run pytest tests/test_discord_config.py tests/test_config_schema_cutover.py -q`

Expected: PASS after old Discord tests are updated to the new namespace.

---

### Task 10: Update Docs, Examples, And Public Config References

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/*.md`
- Modify: `docs/usage/*.md`
- Modify: `docs/plugins/*.md`
- Modify: `CONVENTIONS.md` if it mentions old config names.
- Modify: config examples under `config-examples/`.

- [ ] **Step 1: Search for old vocabulary**

Run: `rg -n "directive|directives|services\\.|\\[services|\\[mcp|mcp_servers|repo_access|sandbox|universal|context_mode|idle_terminate|allowed_users|fallback_model|/workspace/project" README.md docs config-examples src tests`

Expected: output identifies all old references.

- [ ] **Step 2: Update docs**

Replace:

```text
directives -> prompts
services -> tools
mcp/mcp_servers -> tools / tools.<name>.mcp
repo_access -> repo
/workspace/project -> /workspace/repos/<owner>/<repo>
universal -> profiles.base
```

- [ ] **Step 3: Add new config example**

Create or update an example config using the target schema. Do not include old keys.

- [ ] **Step 4: Run documentation search again**

Run: `rg -n "directive|directives|services\\.|\\[services|\\[mcp|mcp_servers|repo_access|sandbox|universal|context_mode|idle_terminate|allowed_users|fallback_model|/workspace/project" README.md docs config-examples`

Expected: no hits except deliberate migration error text or historical notes saying the key was removed.

---

### Task 11: Full Verification And Cleanup

**Files:**
- Modify only files needed to fix test/lint failures.

- [ ] **Step 1: Run targeted config tests**

Run: `uv run pytest tests/test_config_schema_cutover.py tests/test_config_trust.py tests/test_prompts.py tests/test_merge.py tests/test_workspace_config.py tests/test_discord_config.py -q`

Expected: PASS.

- [ ] **Step 2: Run broader affected tests**

Run: `uv run pytest tests/test_worktree.py tests/test_git_sync.py tests/test_container_runner.py tests/test_temporal_scheduler.py tests/test_task_scheduler.py tests/test_workspace_reconcile.py -q`

Expected: PASS.

- [ ] **Step 3: Run lint and formatting**

Run:

```bash
uv run ruff format src tests
uv run ruff check --fix src tests
```

Expected: no remaining ruff errors.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/`

Expected: PASS.

- [ ] **Step 5: Run pre-commit**

Run: `uvx pre-commit run --all-files`

Expected: PASS.

- [ ] **Step 6: Final old-vocabulary audit**

Run: `rg -n "sandbox|sandbox_universal|sandbox_profiles|directives|read_directives|services\\.|\\[services|\\[mcp|mcp_servers|repo_access|context_mode|idle_terminate|allowed_users|fallback_model|/workspace/project" src tests docs README.md config-examples`

Expected: no unintended hits. Any remaining hit must be deliberate migration error text or historical note.

- [ ] **Step 7: Commit the implementation**

Run:

```bash
git status --short
git add src tests docs README.md config-examples prompts
git commit -m "refactor: cut over to capability-oriented config schema"
```

Expected: one coherent commit after all verification passes.

---

## Self-Review

- Spec coverage: Covers workspace slimming, profile composition, prompts rename, tools namespace, tool provider types, repo root/default mounts, repo lists, default connection, job isolation, always idle-terminate containers, and legacy-key rejection.
- Placeholder scan: No planned task relies on unspecified edge handling; each task has concrete files, commands, and expected results.
- Type consistency: The plan consistently uses `prompts`, `tools`, `repo`, `profiles`, `default_core`, `connections`, `ResolvedWorkspaceConfig`, and `/workspace/repos/<owner>/<repo>`.
