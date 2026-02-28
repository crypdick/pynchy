# Sandbox Profiles Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the split directive-scope / workspace-defaults config system with a unified three-tier sandbox profile model.

**Architecture:** New `SandboxProfileConfig` Pydantic model with Optional fields + `model_fields_set` tracking. A `merge_sandbox_config()` function walks the cascade (universal < profile < per-sandbox) producing a `ResolvedSandboxConfig` frozen dataclass. Convention-based directive resolution (`directives/<name>.md`). `"all"` → `"*"` wildcard standardization.

**Tech Stack:** Pydantic v2, Python 3.12, pytest, structlog

---

### Task 1: Add `SandboxProfileConfig` model

**Files:**
- Modify: `src/pynchy/config/models.py:177-184` (replace `WorkspaceDefaultsConfig`)
- Test: `tests/test_models.py` (new file)

**Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
"""Tests for SandboxProfileConfig model."""

from __future__ import annotations

from pynchy.config.models import SandboxProfileConfig


class TestSandboxProfileConfig:
    def test_all_fields_default_to_none(self):
        """Empty profile has no opinions — all fields None."""
        profile = SandboxProfileConfig()
        assert profile.directives is None
        assert profile.skills is None
        assert profile.mcp_servers is None
        assert profile.context_mode is None
        assert profile.access is None
        assert profile.mode is None
        assert profile.trust is None
        assert profile.trigger is None
        assert profile.allowed_users is None
        assert profile.idle_terminate is None
        assert profile.git_policy is None
        assert profile.repo_access is None

    def test_model_fields_set_tracks_explicit(self):
        """Only explicitly set fields appear in model_fields_set."""
        profile = SandboxProfileConfig(skills=["core", "ops"], trust=False)
        assert profile.model_fields_set == {"skills", "trust"}

    def test_list_fields_accept_values(self):
        profile = SandboxProfileConfig(
            directives=["base", "admin-ops"],
            skills=["core"],
            mcp_servers=["gdrive"],
        )
        assert profile.directives == ["base", "admin-ops"]
        assert profile.skills == ["core"]
        assert profile.mcp_servers == ["gdrive"]

    def test_scalar_fields_accept_values(self):
        profile = SandboxProfileConfig(
            context_mode="isolated",
            access="readwrite",
            mode="agent",
            trust=True,
            trigger="always",
            idle_terminate=False,
            git_policy="pull-request",
            repo_access="crypdick/pynchy",
        )
        assert profile.context_mode == "isolated"
        assert profile.idle_terminate is False

    def test_rejects_unknown_fields(self):
        """Strict model rejects typos."""
        import pytest
        with pytest.raises(Exception):
            SandboxProfileConfig(typo_field="oops")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ImportError` (SandboxProfileConfig doesn't exist yet)

**Step 3: Write the model**

In `src/pynchy/config/models.py`, replace `WorkspaceDefaultsConfig` (lines 177-183) with:

```python
class SandboxProfileConfig(_StrictModel):
    """Overridable sandbox config — used for sandbox_universal and sandbox_profiles.

    All fields default to None ("no opinion at this tier, inherit from next").
    Use model_fields_set to distinguish "explicitly set" from "defaulted to None".

    List fields (directives, skills, mcp_servers): unioned across tiers.
    Override fields (all others): most-specific explicitly-set value wins.
    """

    # Union fields (merged across tiers, deduplicated)
    directives: list[str] | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None

    # Override fields (most-specific wins)
    context_mode: Literal["group", "isolated"] | None = None
    access: Literal["read", "write", "readwrite"] | None = None
    mode: Literal["agent", "chat"] | None = None
    trust: bool | None = None
    trigger: Literal["mention", "always"] | None = None
    allowed_users: list[str] | None = None  # override semantics, not union
    idle_terminate: bool | None = None
    git_policy: Literal["merge-to-main", "pull-request"] | None = None
    security: WorkspaceSecurityTomlConfig | None = None
    repo_access: str | None = None
```

Keep `WorkspaceDefaultsConfig` in place for now — we'll remove it in a later task after updating all references.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config/models.py tests/test_models.py
git commit -m "feat: add SandboxProfileConfig model for three-tier config"
```

---

### Task 2: Add `profile` and `directives` fields to `WorkspaceConfig`

**Files:**
- Modify: `src/pynchy/config/models.py:234-255` (WorkspaceConfig)
- Test: `tests/test_models.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
from pynchy.config.models import WorkspaceConfig


class TestWorkspaceConfigNewFields:
    def test_profile_defaults_to_none(self):
        ws = WorkspaceConfig()
        assert ws.profile is None

    def test_directives_defaults_to_none(self):
        ws = WorkspaceConfig()
        assert ws.directives is None

    def test_profile_accepts_string(self):
        ws = WorkspaceConfig(profile="pynchy-dev")
        assert ws.profile == "pynchy-dev"

    def test_directives_accepts_list(self):
        ws = WorkspaceConfig(directives=["ray-bench"])
        assert ws.directives == ["ray-bench"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::TestWorkspaceConfigNewFields -v`
Expected: FAIL (fields don't exist yet)

**Step 3: Add the fields**

In `WorkspaceConfig` (models.py), add after the `name` field (line 236):

```python
    profile: str | None = None  # sandbox_profiles.<name> reference
    directives: list[str] | None = None  # directive names; convention: directives/<name>.md
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config/models.py tests/test_models.py
git commit -m "feat: add profile and directives fields to WorkspaceConfig"
```

---

### Task 3: Create `merge.py` with merge function and `ResolvedSandboxConfig`

**Files:**
- Create: `src/pynchy/config/merge.py`
- Create: `tests/test_merge.py`

**Step 1: Write the failing tests**

Create `tests/test_merge.py`:

```python
"""Tests for sandbox config merge logic."""

from __future__ import annotations

import pytest

from pynchy.config.merge import ResolvedSandboxConfig, merge_sandbox_config
from pynchy.config.models import SandboxProfileConfig, WorkspaceConfig


class TestMergeUnionFields:
    """List fields (directives, skills, mcp_servers) are unioned across tiers."""

    def test_union_from_all_tiers(self):
        universal = SandboxProfileConfig(directives=["base"], skills=["core"])
        profile = SandboxProfileConfig(directives=["admin-ops"], skills=["ops"])
        sandbox = WorkspaceConfig(
            chat="connection.slack.s.chat.c",
            directives=["extra"],
            skills=["custom-skill"],
        )
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.directives == ["base", "admin-ops", "extra"]
        assert result.skills == ["core", "ops", "custom-skill"]

    def test_union_deduplicates(self):
        universal = SandboxProfileConfig(skills=["core"])
        profile = SandboxProfileConfig(skills=["core", "ops"])
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.skills == ["core", "ops"]

    def test_none_means_empty_for_union(self):
        """None at a tier contributes nothing to the union."""
        universal = SandboxProfileConfig(directives=["base"])
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.directives == ["base"]
        assert result.skills == []
        assert result.mcp_servers == []

    def test_no_profile(self):
        universal = SandboxProfileConfig(directives=["base"])
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.directives == ["base"]


class TestMergeOverrideFields:
    """Scalar fields use most-specific-wins semantics."""

    def test_sandbox_overrides_profile(self):
        universal = SandboxProfileConfig(trigger="mention")
        profile = SandboxProfileConfig(trigger="always")
        sandbox = WorkspaceConfig(
            chat="connection.slack.s.chat.c",
            trigger="mention",
        )
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.trigger == "mention"  # sandbox wins

    def test_profile_overrides_universal(self):
        universal = SandboxProfileConfig(idle_terminate=True)
        profile = SandboxProfileConfig(idle_terminate=False)
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.idle_terminate is False  # profile wins

    def test_universal_provides_default(self):
        universal = SandboxProfileConfig(context_mode="group")
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.context_mode == "group"

    def test_no_tier_sets_field_uses_hardcoded_default(self):
        universal = SandboxProfileConfig()
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.idle_terminate is True  # hardcoded default
        assert result.context_mode == "group"  # hardcoded default

    def test_allowed_users_override_not_union(self):
        """allowed_users uses override semantics, not union."""
        universal = SandboxProfileConfig(allowed_users=["*"])
        profile = SandboxProfileConfig(allowed_users=["alice", "bob"])
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.allowed_users == ["alice", "bob"]  # profile wins, not ["*", "alice", "bob"]


class TestMergePassthrough:
    """Fields that only exist on WorkspaceConfig pass through unchanged."""

    def test_chat_passes_through(self):
        universal = SandboxProfileConfig()
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.admin-1", is_admin=True)
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.chat == "connection.slack.s.chat.admin-1"
        assert result.is_admin is True

    def test_schedule_passes_through(self):
        universal = SandboxProfileConfig()
        sandbox = WorkspaceConfig(
            chat="connection.slack.s.chat.c",
            schedule="0 */1 * * *",
            prompt="Run code improvement.",
        )
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.schedule == "0 */1 * * *"
        assert result.prompt == "Run code improvement."

    def test_repo_access_from_profile(self):
        """repo_access can be set at the profile level."""
        universal = SandboxProfileConfig()
        profile = SandboxProfileConfig(repo_access="crypdick/pynchy")
        sandbox = WorkspaceConfig(chat="connection.slack.s.chat.c")
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.repo_access == "crypdick/pynchy"

    def test_repo_access_sandbox_overrides_profile(self):
        profile = SandboxProfileConfig(repo_access="crypdick/pynchy")
        sandbox = WorkspaceConfig(
            chat="connection.slack.s.chat.c",
            repo_access="other/repo",
        )
        result = merge_sandbox_config(SandboxProfileConfig(), profile, sandbox)
        assert result.repo_access == "other/repo"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_merge.py -v`
Expected: FAIL with `ImportError`

**Step 3: Implement merge.py**

Create `src/pynchy/config/merge.py`:

```python
"""Sandbox config merge — three-tier cascade producing a resolved config.

Cascade order (most specific wins):
    sandbox_universal < sandbox_profile < per-sandbox

List fields (directives, skills, mcp_servers) are unioned across tiers.
Override fields use most-specific-wins semantics via model_fields_set.

After this merge, the connection/chat security cascade in access.py
applies on top for channel-specific fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pynchy.config.models import SandboxProfileConfig, WorkspaceConfig, WorkspaceSecurityTomlConfig
from pynchy.logger import logger

# Fields that are unioned (merged) across tiers.
_UNION_FIELDS = ("directives", "skills", "mcp_servers")

# Fields that use override semantics (most-specific wins).
# These exist on both SandboxProfileConfig and WorkspaceConfig.
_OVERRIDE_FIELDS = (
    "context_mode",
    "access",
    "mode",
    "trust",
    "trigger",
    "allowed_users",
    "idle_terminate",
    "git_policy",
    "security",
    "repo_access",
)

# Hardcoded defaults when no tier sets a value.
_HARDCODED_DEFAULTS: dict[str, Any] = {
    "context_mode": "group",
    "access": "readwrite",
    "mode": "agent",
    "trust": True,
    "trigger": "mention",
    "allowed_users": ["owner"],
    "idle_terminate": True,
    "git_policy": "merge-to-main",
    "security": None,
    "repo_access": None,
}


@dataclass(frozen=True)
class ResolvedSandboxConfig:
    """Fully-resolved sandbox config after three-tier merge.

    Downstream code consumes this instead of querying WorkspaceConfig +
    resolve_directives() separately.
    """

    # Union fields
    directives: list[str]
    skills: list[str]
    mcp_servers: list[str]

    # Override fields
    context_mode: str
    access: str
    mode: str
    trust: bool
    trigger: str
    allowed_users: list[str]
    idle_terminate: bool
    git_policy: str
    security: WorkspaceSecurityTomlConfig | None
    repo_access: str | None

    # Pass-through from WorkspaceConfig (not overridable by profiles)
    chat: str | None
    is_admin: bool
    schedule: str | None
    prompt: str | None
    name: str | None
    mcp: dict[str, dict[str, Any]]


def _union_lists(*sources: list[str] | None) -> list[str]:
    """Merge lists with deduplication, preserving insertion order."""
    seen: set[str] = set()
    result: list[str] = []
    for source in sources:
        if source is None:
            continue
        for item in source:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _resolve_override(
    field: str,
    sandbox: WorkspaceConfig,
    profile: SandboxProfileConfig | None,
    universal: SandboxProfileConfig,
    sandbox_name: str,
) -> Any:
    """Resolve an override field: most-specific explicitly-set value wins.

    Cascade: per-sandbox > profile > universal > hardcoded default.
    """
    tiers: list[tuple[str, object]] = [
        ("per-sandbox", sandbox),
        ("profile", profile),
        ("universal", universal),
    ]

    sources_log: dict[str, Any] = {}
    for tier_name, tier_obj in tiers:
        if tier_obj is None:
            continue
        if field in tier_obj.model_fields_set:
            value = getattr(tier_obj, field)
            sources_log[tier_name] = value

    # Walk from most specific to least specific
    for tier_name, tier_obj in tiers:
        if tier_obj is None:
            continue
        if field in tier_obj.model_fields_set:
            effective = getattr(tier_obj, field)
            # Log overrides (when multiple tiers set the same field)
            overridden = {k: v for k, v in sources_log.items() if k != tier_name}
            if overridden:
                logger.info(
                    "config.merge.override",
                    sandbox=sandbox_name,
                    field=field,
                    effective=effective,
                    source=tier_name,
                    overridden=overridden,
                )
            else:
                logger.debug(
                    "config.merge.resolve",
                    sandbox=sandbox_name,
                    field=field,
                    effective=effective,
                    source=tier_name,
                )
            return effective

    # No tier set it — use hardcoded default
    default = _HARDCODED_DEFAULTS[field]
    logger.debug(
        "config.merge.resolve",
        sandbox=sandbox_name,
        field=field,
        effective=default,
        source="hardcoded-default",
    )
    return default


def merge_sandbox_config(
    universal: SandboxProfileConfig,
    profile: SandboxProfileConfig | None,
    sandbox: WorkspaceConfig,
) -> ResolvedSandboxConfig:
    """Merge three config tiers into a fully-resolved sandbox config.

    Cascade: sandbox_universal < sandbox_profile < per-sandbox.

    Args:
        universal: The [sandbox_universal] config (applies to all sandboxes).
        profile: The [sandbox_profiles.<name>] config, or None if no profile.
        sandbox: The [sandbox.<folder>] config (most specific).

    Returns:
        Frozen dataclass with all fields resolved.
    """
    sandbox_name = sandbox.name or "unknown"

    # Resolve union fields
    union_results: dict[str, list[str]] = {}
    for field in _UNION_FIELDS:
        u_val = getattr(universal, field, None)
        p_val = getattr(profile, field, None) if profile else None
        s_val = getattr(sandbox, field, None)
        merged = _union_lists(u_val, p_val, s_val)
        union_results[field] = merged

        sources = {}
        if u_val:
            sources["universal"] = u_val
        if p_val:
            sources["profile"] = p_val
        if s_val:
            sources["per-sandbox"] = s_val
        logger.debug(
            "config.merge.union",
            sandbox=sandbox_name,
            field=field,
            effective=merged,
            sources=sources,
        )

    # Resolve override fields
    override_results: dict[str, Any] = {}
    for field in _OVERRIDE_FIELDS:
        override_results[field] = _resolve_override(
            field, sandbox, profile, universal, sandbox_name
        )

    return ResolvedSandboxConfig(
        # Union fields
        directives=union_results["directives"],
        skills=union_results["skills"],
        mcp_servers=union_results["mcp_servers"],
        # Override fields
        **override_results,
        # Pass-through
        chat=sandbox.chat,
        is_admin=sandbox.is_admin,
        schedule=sandbox.schedule,
        prompt=sandbox.prompt,
        name=sandbox.name,
        mcp=sandbox.mcp,
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config/merge.py tests/test_merge.py
git commit -m "feat: add merge_sandbox_config() with union and override semantics"
```

---

### Task 4: Rewrite directive resolution to convention-based

**Files:**
- Modify: `src/pynchy/config/directives.py`
- Modify: `tests/test_directives.py`

**Step 1: Write the failing tests**

Replace `tests/test_directives.py` entirely:

```python
"""Tests for convention-based directive resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.directives import read_directives


class TestReadDirectives:
    @pytest.fixture()
    def directives_dir(self, tmp_path: Path) -> Path:
        """Create a temp directives/ directory with markdown files."""
        d = tmp_path / "directives"
        d.mkdir()
        (d / "base.md").write_text("# Base\nShared instructions.")
        (d / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        (d / "repo-dev.md").write_text("# Repo Dev\nRepo-specific content.")
        return tmp_path

    def test_reads_single_directive(self, directives_dir: Path):
        result = read_directives(["base"], directives_dir)
        assert result == "# Base\nShared instructions."

    def test_reads_multiple_directives(self, directives_dir: Path):
        result = read_directives(["base", "admin-ops"], directives_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result
        assert "---" in result  # separator

    def test_preserves_order(self, directives_dir: Path):
        result = read_directives(["admin-ops", "base"], directives_dir)
        assert result is not None
        assert result.index("Admin Ops") < result.index("Base")

    def test_empty_list_returns_none(self, directives_dir: Path):
        result = read_directives([], directives_dir)
        assert result is None

    def test_missing_file_warns_and_skips(self, directives_dir: Path):
        result = read_directives(["nonexistent"], directives_dir)
        assert result is None

    def test_missing_file_among_valid(self, directives_dir: Path):
        result = read_directives(["base", "nonexistent", "admin-ops"], directives_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result

    def test_empty_file_skipped(self, directives_dir: Path):
        (directives_dir / "directives" / "empty.md").write_text("")
        result = read_directives(["empty"], directives_dir)
        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_directives.py -v`
Expected: FAIL with `ImportError` (read_directives doesn't exist)

**Step 3: Rewrite directives.py**

Replace `src/pynchy/config/directives.py`:

```python
"""Convention-based directive resolution — reads directives/<name>.md files.

Directive names map to files by convention: "base" → directives/base.md.
No scope logic — assignment is handled by sandbox profiles.

Usage::

    from pynchy.config.directives import read_directives

    text = read_directives(["base", "admin-ops"], project_root)
"""

from __future__ import annotations

from pathlib import Path

from pynchy.logger import logger


def read_directives(names: list[str], project_root: Path) -> str | None:
    """Read and concatenate directive files by name.

    Maps each name to ``directives/<name>.md`` under *project_root*.
    Missing or empty files are warned about and skipped.

    Returns None if no directives match (or all are missing/empty).
    """
    if not names:
        return None

    parts: list[str] = []

    for name in names:
        file_path = project_root / "directives" / f"{name}.md"
        if not file_path.exists():
            logger.warning(
                "Directive file not found, skipping",
                directive=name,
                path=str(file_path),
            )
            continue

        content = _read_file(file_path)
        if content:
            parts.append(content)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


def _read_file(path: Path) -> str | None:
    """Read a file, returning None on error or empty content."""
    try:
        text = path.read_text().strip()
        return text if text else None
    except OSError:
        logger.warning("Failed to read directive file", path=str(path))
        return None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_directives.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config/directives.py tests/test_directives.py
git commit -m "refactor: rewrite directive resolution to convention-based (no scopes)"
```

---

### Task 5: Update Settings to use new config sections

**Files:**
- Modify: `src/pynchy/config/settings.py:36-179`
- Modify: `src/pynchy/config/models.py` (delete `DirectiveConfig`, `WorkspaceDefaultsConfig`)
- Modify: `tests/conftest.py:45-82`
- Modify: `tests/test_channel_access.py` (update imports)
- Modify: `tests/test_reconciler.py` (update imports)
- Modify: `tests/test_workspace_config.py` (update imports)

**Step 1: Update Settings class**

In `src/pynchy/config/settings.py`:

1. Replace import of `DirectiveConfig` and `WorkspaceDefaultsConfig` with `SandboxProfileConfig`:
   ```python
   from pynchy.config.models import (
       ...
       SandboxProfileConfig,
       # Remove: DirectiveConfig, WorkspaceDefaultsConfig
       ...
   )
   ```

2. Replace the `workspace_defaults` and `directives` fields (lines 162, 179):
   ```python
   sandbox_universal: SandboxProfileConfig = SandboxProfileConfig()
   sandbox_profiles: dict[str, SandboxProfileConfig] = {}
   # Remove: workspace_defaults, directives
   ```

3. Add profile reference validator (new `@model_validator`):
   ```python
   @model_validator(mode="after")
   def _validate_profile_refs(self) -> Settings:
       """Validate that sandbox profile references exist."""
       for folder, ws in self.workspaces.items():
           if ws.profile is not None and ws.profile not in self.sandbox_profiles:
               raise ValueError(
                   f"sandbox.{folder}.profile references unknown profile: "
                   f"'{ws.profile}'. Available: {list(self.sandbox_profiles.keys())}"
               )
       return self
   ```

4. Update `_validate_admin_clean_room` to replace `"all"` with `"*"` (line 322-323):
   ```python
   if entry == "*":
       resolved.update(self.mcp_servers.keys())
   ```

**Step 2: Delete old models**

In `src/pynchy/config/models.py`:
- Delete `WorkspaceDefaultsConfig` class (lines 177-183)
- Delete `DirectiveConfig` class (lines 366-378)

**Step 3: Update conftest.py**

In `tests/conftest.py`, update the `make_settings` helper:
- Replace `WorkspaceDefaultsConfig` import with `SandboxProfileConfig`
- Replace `"workspace_defaults": WorkspaceDefaultsConfig()` with `"sandbox_universal": SandboxProfileConfig()`

**Step 4: Update test files that import the deleted models**

- `tests/test_channel_access.py`: Replace `WorkspaceDefaultsConfig` with `SandboxProfileConfig`
- `tests/test_reconciler.py`: Replace `WorkspaceDefaultsConfig` with `SandboxProfileConfig`
- `tests/test_workspace_config.py`: Replace `WorkspaceDefaultsConfig` with `SandboxProfileConfig`

**Step 5: Run tests**

Run: `uv run pytest tests/ -v`
Expected: Some tests will need further fixups in access.py (next task). Fix any remaining import errors first.

**Step 6: Commit**

```bash
git add src/pynchy/config/settings.py src/pynchy/config/models.py tests/conftest.py tests/test_channel_access.py tests/test_reconciler.py tests/test_workspace_config.py
git commit -m "refactor: replace workspace_defaults and directives with sandbox_universal and sandbox_profiles"
```

---

### Task 6: Update access cascade

**Files:**
- Modify: `src/pynchy/config/access.py`
- Modify: `tests/test_channel_access.py`

**Step 1: Update access.py**

The cascade docstring and Layer 0 need updating. The new cascade order is:

```
sandbox_universal < profile < per-sandbox < connection.security < chat.security
```

In `resolve_channel_config()`:

```python
def resolve_channel_config(
    workspace_name: str,
    channel_jid: str | None = None,
    channel_plugin_name: str | None = None,
) -> ResolvedChannelConfig:
    """Walk the resolution cascade and return a fully-resolved config.

    Cascade (most specific wins):
    1. connection.<type>.<name>.chat.*.security (chat-level overrides)
    2. connection.<type>.<name>.security (connection-level overrides)
    3. sandbox.<name>.* (workspace overrides)
    4. sandbox_profiles.<name>.* (profile defaults)
    5. sandbox_universal.* (global defaults)
    """
    from pynchy.config.merge import merge_sandbox_config
    from pynchy.types import ResolvedChannelConfig

    s = get_settings()
    ws = s.workspaces.get(workspace_name)

    # Layer 0: merge universal + profile + per-sandbox
    profile = None
    if ws is not None and ws.profile:
        profile = s.sandbox_profiles.get(ws.profile)

    merged = merge_sandbox_config(
        s.sandbox_universal,
        profile,
        ws or WorkspaceConfig(),
    )

    state: dict = {
        "access": merged.access,
        "mode": merged.mode,
        "trust": merged.trust,
        "trigger": merged.trigger,
        "allowed_users": merged.allowed_users,
    }

    # Layer 1+2: connection and chat-level overrides (most specific)
    if ws is not None:
        chat_ref = parse_chat_ref(ws.chat)
        if chat_ref is not None:
            conn_cfg = s.connection.get_connection(chat_ref.platform, chat_ref.name)
            if conn_cfg and conn_cfg.security:
                _apply_overrides(state, conn_cfg.security)
            if conn_cfg:
                chat_cfg = conn_cfg.chat.get(chat_ref.chat)
                if chat_cfg and chat_cfg.security:
                    _apply_overrides(state, chat_cfg.security)

    return ResolvedChannelConfig(**state)
```

**Step 2: Update access.py imports**

Add `WorkspaceConfig` import (needed for fallback `WorkspaceConfig()`).

**Step 3: Update channel access tests**

Replace `WorkspaceDefaultsConfig` with `SandboxProfileConfig` in test fixtures. Update `make_settings()` calls to use `sandbox_universal=` instead of `workspace_defaults=`.

**Step 4: Run tests**

Run: `uv run pytest tests/test_channel_access.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config/access.py tests/test_channel_access.py
git commit -m "refactor: update access cascade to use sandbox_universal + profile merge"
```

---

### Task 7: Update `workspace_config.py` and `agent_runner.py` callsites

**Files:**
- Modify: `src/pynchy/host/orchestrator/workspace_config.py:149-188`
- Modify: `src/pynchy/host/orchestrator/agent_runner.py:157-177`
- Modify: `src/pynchy/host/container_manager/mounts.py:59-63`

**Step 1: Update workspace_config.py**

`load_workspace_config()` should now return a `ResolvedSandboxConfig` or make it available. Update `get_repo_access()` to use the merged config.

```python
def load_workspace_config(group_folder: str) -> WorkspaceConfig | None:
    """Read workspace config for a group from Settings.

    Returns None if the group has no [sandbox.<folder>] section in config.toml.
    """
    specs = _workspace_specs()
    spec = specs.get(group_folder)
    if spec is None:
        return None
    return spec.config


def load_resolved_config(group_folder: str) -> ResolvedSandboxConfig | None:
    """Load and merge the full config cascade for a sandbox.

    Returns None if the group has no config.
    """
    from pynchy.config.merge import ResolvedSandboxConfig, merge_sandbox_config

    ws = load_workspace_config(group_folder)
    if ws is None:
        return None

    s = get_settings()
    profile = None
    if ws.profile:
        profile = s.sandbox_profiles.get(ws.profile)

    return merge_sandbox_config(s.sandbox_universal, profile, ws)
```

**Step 2: Update agent_runner.py**

In `_pre_container_setup()` (line 169-177), replace:

```python
from pynchy.config.directives import resolve_directives
from pynchy.host.orchestrator.workspace_config import get_repo_access

...
repo_access = get_repo_access(group)
system_prompt_append = resolve_directives(group.folder, repo_access)
```

With:

```python
from pynchy.config.directives import read_directives
from pynchy.host.orchestrator.workspace_config import load_resolved_config

...
resolved = load_resolved_config(group.folder)
if repo_access_override is not None:
    repo_access = repo_access_override
else:
    repo_access = resolved.repo_access if resolved else None
system_prompt_append = read_directives(
    resolved.directives if resolved else [],
    get_settings().project_root,
)
```

**Step 3: Update mounts.py**

In `_prepare_session_dir()` (line 59-63), update to use resolved config:

```python
from pynchy.host.orchestrator.workspace_config import load_resolved_config

...
resolved = load_resolved_config(group.folder)
_sync_skills(
    session_dir,
    plugin_manager,
    workspace_skills=resolved.skills if resolved else None,
)
```

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (or known failures from remaining tasks)

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/workspace_config.py src/pynchy/host/orchestrator/agent_runner.py src/pynchy/host/container_manager/mounts.py
git commit -m "refactor: wire callsites to use merged sandbox config and convention-based directives"
```

---

### Task 8: Standardize `"all"` → `"*"` wildcard

**Files:**
- Modify: `src/pynchy/host/container_manager/session_prep.py:60-78`
- Modify: `tests/test_session_prep.py` (if exists)

**Step 1: Write the failing test**

Check if session_prep tests exist and add a test for `"*"`:

```python
def test_star_wildcard_includes_everything():
    assert _is_skill_selected("any-skill", "community", ["*"])
    assert _is_skill_selected("any-skill", "experimental", ["*"])
```

**Step 2: Update `_is_skill_selected`**

In `session_prep.py`, line 66:

```python
# Before:
if "all" in workspace_skills:
    return True

# After:
if "*" in workspace_skills:
    return True
```

**Step 3: Run tests**

Run: `uv run pytest tests/ -v -k "skill"`
Expected: PASS

**Step 4: Commit**

```bash
git add src/pynchy/host/container_manager/session_prep.py
git commit -m "refactor: standardize wildcard from 'all' to '*' in skill selection"
```

---

### Task 9: Rename `admin-ops` → `pynchy-admin-ops` directive

**Files:**
- Rename: `directives/admin-ops.md` → `directives/pynchy-admin-ops.md`

**Step 1: Rename the file**

```bash
git mv directives/admin-ops.md directives/pynchy-admin-ops.md
```

**Step 2: Commit**

```bash
git commit -m "refactor: rename admin-ops directive to pynchy-admin-ops"
```

---

### Task 10: Update config example template

**Files:**
- Modify: `config-examples/config.toml.EXAMPLE`

**Step 1: Update the template**

Replace the `[workspace_defaults]` section (lines 168-179) with:

```toml
# ─────────────────────────────────────────────────────────────────────────────
# Sandbox Universal (defaults for all sandboxes)
# ─────────────────────────────────────────────────────────────────────────────
# These values apply to every sandbox. Per-sandbox and profile values override.

# [sandbox_universal]
# directives = ["base", "idle-escape"]
# context_mode = "group"
# access = "readwrite"   # "read", "write", "readwrite"
# mode = "agent"         # "agent" or "chat"
# trust = true
# trigger = "mention"    # "mention" or "always"
# allowed_users = ["owner"]  # user IDs, group refs, or "*"
```

Replace the `[directives.*]` section (lines 445-474) with:

```toml
# ─────────────────────────────────────────────────────────────────────────────
# Sandbox Profiles (reusable config bundles)
# ─────────────────────────────────────────────────────────────────────────────
# Profiles bundle directives, skills, MCP servers, and other config.
# Sandboxes reference a profile via `profile = "<name>"`.
# Effective config = sandbox_universal ∪ profile ∪ per-sandbox.
#
# List fields (directives, skills, mcp_servers) are unioned across tiers.
# Scalar fields use most-specific-wins semantics.
#
# Directives use convention-based resolution: name "base" → directives/base.md.

# [sandbox_profiles.pynchy-dev]
# directives = ["pynchy-admin-ops", "pynchy-code-improver"]
# skills = ["core", "ops"]
# repo_access = "owner/repo"
# idle_terminate = false
# trigger = "always"

# [sandbox_profiles.ray-bench]
# directives = ["ray-bench"]
# repo_access = "owner/ray-bench"
# mcp_servers = ["notebook"]
```

Update the sandbox examples to show `profile` field:

```toml
# [sandbox.admin-1]
# profile = "pynchy-dev"
# chat = "connection.slack.synapse.chat.admin-1"
# is_admin = true
```

**Step 2: Run mkdocs build**

Run: `uv run mkdocs build --strict`
Expected: PASS (config example isn't rendered by mkdocs, but ensures no other docs break)

**Step 3: Commit**

```bash
git add config-examples/config.toml.EXAMPLE
git commit -m "docs: update config example template for sandbox profiles"
```

---

### Task 11: Run full test suite and fix remaining issues

**Files:**
- Various (depends on failures)

**Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`

**Step 2: Fix any failures**

Common issues to watch for:
- Tests that directly construct `Settings` with `workspace_defaults=` or `directives=`
- Tests that mock `get_settings()` and set `.workspace_defaults`
- Tests that import `DirectiveConfig` or `WorkspaceDefaultsConfig`

**Step 3: Run mkdocs build**

Run: `uv run mkdocs build --strict`

**Step 4: Commit**

```bash
git add -A
git commit -m "fix: resolve remaining test failures from sandbox profiles migration"
```

---

### Task 12: Migrate production config on pynchy-server

**Files:**
- Production `config.toml` on pynchy-server

**IMPORTANT:** Use the @pynchy-ops skill for deployment procedures.

**Step 1: SSH to pynchy-server and backup config**

```bash
ssh pynchy-server 'cp ~/src/PERSONAL/pynchy/config.toml ~/src/PERSONAL/pynchy/config.toml.bak'
```

**Step 2: Edit production config.toml**

Replace `[workspace_defaults]` and `[directives.*]` sections with the new `[sandbox_universal]`, `[sandbox_profiles.*]` format, and add `profile` references to sandbox sections. See the design doc "After" section for the exact config.

Key changes:
- `[workspace_defaults]` → `[sandbox_universal]` with `directives = ["base", "idle-escape"]`
- All `[directives.*]` sections → deleted
- `[sandbox_profiles.pynchy-dev]` with shared admin config
- `[sandbox_profiles.ray-bench]` with shared ray-bench config
- Admin sandboxes get `profile = "pynchy-dev"`
- Ray-bench sandboxes get `profile = "ray-bench"`
- `skills = ["all"]` → `skills = ["*"]` if present anywhere

**Step 3: Deploy and verify**

Follow the pynchy-ops skill for deployment.

**Step 4: Commit production config change** (auto-deploy handles this)

---

### Task 13: Update documentation using docs skill

**IMPORTANT:** Use the @docs-manager skill for documentation procedures.

**Files:**
- Docs pages referencing `[directives.*]`, `[workspace_defaults]`, or directive scoping

**Step 1: Identify docs pages to update**

Search docs/ for references to the old config model:
```bash
grep -r "directives\.\|workspace_defaults\|scope.*all\|scope.*repo" docs/
```

**Step 2: Update each page**

Update references to reflect the new sandbox profiles model. Key concepts to document:
- Three-tier merge model
- Convention-based directive resolution
- Profile references
- `"*"` wildcard standardization

**Step 3: Build and verify**

Run: `uv run mkdocs build --strict`
Expected: PASS

**Step 4: Commit**

```bash
git add docs/
git commit -m "docs: update architecture and config docs for sandbox profiles"
```

---

### Task 14: Final verification

**Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS

**Step 2: Run mkdocs build**

Run: `uv run mkdocs build --strict`
Expected: PASS

**Step 3: Verify production service** (if deployed)

Check pynchy-server logs for clean startup with the new config.
