# Skill Scoping Design

**Date**: 2026-02-26
**Status**: Approved

## Problem

All workspaces receive every skill, wasting context window tokens and adding noise. Skills like `code-improver` (pynchy-specific), `slack-token-extractor` (ops-only), and `x-integration` (unused) are loaded into workspaces that have no use for them.

## Changes

### 1. Flip the default: `skills=None` → core-only

In `_is_skill_selected()`, change the `workspace_skills is None` branch from "include everything" to "core tier only". Workspaces that need non-core skills must explicitly opt in.

**File**: `src/pynchy/container_runner/_session_prep.py`

### 2. Convert code-improver skill → pynchy-code-improver directive

The code-improver skill is workflow instructions specific to the pynchy repo. It belongs as a directive scoped to `crypdick/pynchy`, not a globally-discoverable skill.

- Delete `container/skills/code-improver/`
- Create `directives/pynchy-code-improver.md` with the content (minus YAML frontmatter)
- Add config:
  ```toml
  [directives.pynchy-code-improver]
  file = "directives/pynchy-code-improver.md"
  scope = "crypdick/pynchy"
  ```

### 3. Change x-integration tier from `ops` → `social`

Prevents x-integration from being included when a workspace opts into `ops` tier. No workspace currently needs it.

**File**: `container/skills/x-integration/SKILL.md`

### 4. Add explicit skill declarations to admin workspaces

```toml
[sandbox.admin-1]
skills = ["core", "ops"]

[sandbox.admin-2]
skills = ["core", "ops"]
```

### 5. Update tests

Update `_is_skill_selected` tests to reflect the new default (None → core-only instead of None → all).

## Result

| Workspace | Skills | Directives |
|-----------|--------|------------|
| admin-1/2 | python-heredoc, slack-token-extractor | base, idle-escape, admin-ops, pynchy-code-improver |
| code-improver | python-heredoc | base, idle-escape, pynchy-code-improver |
| gantt-1/2 | python-heredoc | base, idle-escape |
| ray-bench-1/2/3 | python-heredoc | base, idle-escape |
| anyscale-1 | python-heredoc | base, idle-escape |
