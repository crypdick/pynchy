# Skill Scoping Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Scope skills to only the workspaces that need them, convert code-improver to a directive, and flip the default from "all skills" to "core only".

**Architecture:** Change the `_is_skill_selected()` default so omitting `skills` in config means core-only. Move code-improver content from a skill to a directive scoped to `crypdick/pynchy`. Re-tier x-integration so it's excluded from `ops`. Add explicit `skills` declarations to admin workspaces.

**Tech Stack:** Python, TOML config, pytest

---

### Task 1: Change `_is_skill_selected()` default to core-only

**Files:**
- Modify: `src/pynchy/container_runner/_session_prep.py:60-78`

**Step 1: Update `_is_skill_selected()`**

In `src/pynchy/container_runner/_session_prep.py`, change the `None` branch:

```python
def _is_skill_selected(name: str, tier: str, workspace_skills: list[str] | None) -> bool:
    """Determine whether a skill should be included for a workspace.

    Resolution rules:
    - ``workspace_skills is None`` → core only (safe default)
    - ``"all"`` in the list → include everything
    - Tier matches an entry → include
    - Name matches an entry → include
    - ``tier == "core"`` → always included when any filtering is active
    """
    if workspace_skills is None:
        return tier == "core"
    if "all" in workspace_skills:
        return True
    if tier in workspace_skills:
        return True
    if name in workspace_skills:
        return True
    return tier == "core"
```

The only change is line `return True` → `return tier == "core"` in the `None` branch.

**Step 2: Commit**

```bash
git add src/pynchy/container_runner/_session_prep.py
git commit -m "feat: change skill default from all to core-only

Workspaces that omit the skills config field now only get core-tier
skills instead of everything. Workspaces that need non-core skills
must explicitly opt in with skills = [\"core\", \"ops\"] etc."
```

---

### Task 2: Update tests for new default

**Files:**
- Modify: `tests/test_container_runner.py`

**Step 1: Update `TestIsSkillSelected.test_none_includes_everything`**

Rename and fix the test at line 1365:

```python
def test_none_is_core_only(self):
    """skills=None means core-only (safe default)."""
    assert _is_skill_selected("any-skill", "community", None) is False
    assert _is_skill_selected("browser", "core", None) is True
```

**Step 2: Update `TestSyncSkillsFiltering.test_none_copies_all_skills`**

Rename and fix the test at line 1408:

```python
def test_none_copies_core_only(self, tmp_path: Path):
    """workspace_skills=None copies only core-tier skills (safe default)."""
    skills_src = tmp_path / "container" / "skills"
    self._create_skill(skills_src, "browser", "core")
    self._create_skill(skills_src, "improver", "dev")
    self._create_skill(skills_src, "extra", "community")

    session_dir = tmp_path / "session" / ".claude"
    session_dir.mkdir(parents=True)

    with _patch_settings(tmp_path):
        _sync_skills(session_dir, workspace_skills=None)

    copied = {d.name for d in (session_dir / "skills").iterdir() if d.is_dir()}
    assert copied == {"browser"}
```

**Step 3: Run tests to verify**

Run: `uv run pytest tests/test_container_runner.py::TestIsSkillSelected tests/test_container_runner.py::TestSyncSkillsFiltering -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add tests/test_container_runner.py
git commit -m "test: update skill selection tests for core-only default"
```

---

### Task 3: Change x-integration tier from `ops` to `social`

**Files:**
- Modify: `container/skills/x-integration/SKILL.md:4`

**Step 1: Update the tier**

Change line 4 from `tier: ops` to `tier: social`:

```yaml
---
name: x-integration
description: Post tweets, like, reply, retweet, and quote on X (Twitter) using browser automation. Use when the user asks you to interact with X/Twitter.
tier: social
---
```

**Step 2: Commit**

```bash
git add container/skills/x-integration/SKILL.md
git commit -m "chore: re-tier x-integration from ops to social

Prevents x-integration from being included when a workspace opts
into the ops tier. No workspace currently needs this skill."
```

---

### Task 4: Convert code-improver skill to pynchy-code-improver directive

**Files:**
- Delete: `container/skills/code-improver/` (entire directory)
- Create: `directives/pynchy-code-improver.md`

**Step 1: Create the directive file**

Create `directives/pynchy-code-improver.md` with the content from `container/skills/code-improver/SKILL.md`, minus the YAML frontmatter:

```markdown
# Pynchy Core Code Improver

## Rules

- If something looks over-engineered, think about how to simplify it
- **Prefer simplification and code removal**: If you find legacy fallbacks, backwards compatibility shims, or deprecated patterns, prefer to delete them and use the latest pattern. Reduce bloat by removing code rather than adding more.
- If there are parallel implementations for the same functionality, consolidate them. But don't be too pedantic about it; if it's just a couple of lines of code that appear in a couple of places, it's usually not worth the effort.
- If a change requires design input, message the human and stop (unless you are triggered by cron)
- Prefix all commits with `[code improver]`
- Run tests before committing: `uv run pytest tests/`
- Fix warnings in tests.
- Run linting before committing: `uv run ruff check --fix src/ container/agent_runner/src/`
- Never make purely cosmetic changes
- Don't make 'god' modules. Files should generally max out around ~450 lines. Files much larger than this should be refactored.
- Keep docs and comments up to date in accordance to the [contributing-docs.md](../../docs/contributing/contributing-docs.md) file.
- making sure plugin-specific code doesn't leak into the core codebase; it should stay with the plugin.
- remove overly defensive try/except blocks that swallow errors. we should only swallow try/except errors for expected errors during normal operation, not to sweep potential bugs under the rug.

## Production Architecture

**You are running in a container environment:**

1. **Host system** - The physical/VM machine
2. **Main Pynchy process** - Runs directly on the host (runs WhatsApp, scheduler, etc.)
3. **Agent container (YOU)** - Your isolated execution environment, spawned by the main process

When you run tests or code, you're executing inside the agent container, which is isolated from the host system. This means:

- System libraries installed on the host (like libmagic1) are NOT accessible to you
- Dependencies must be installed in the agent container's Python environment
- File access is limited to explicitly mounted paths like /workspace
- Tests that require system libraries (e.g., neonize's libmagic dependency) will fail unless those libraries are baked into the agent container image

## Working Directory

The project source is at /workspace/project. Always work from there.
Treat `/workspace/project` as the pynchy core repo and avoid making cross-repo/plugin changes unless explicitly requested.

## Scheduled Run Workflow

When triggered by a scheduled run:

1. Check `git log --oneline -1`. If the last commit message starts with
   "[code improver] no improvements needed", exit immediately - nothing has
   changed since your last run.

2. Run `git log --oneline -20` to see recent history and your past work.

3. Review the codebase for concrete improvements. Pick the single most important
   code improvement you can make in this session and execute it. Possibilities
   include: dead code, duplicated logic, inconsistent patterns, missing error
   handling, type safety gaps, bugs, improved abstractions, improving test
   coverage, or any other kind of code improvement.

4. If something looks like an over-engineered mess, pause and ponder how to make
   it more elegant.

5. If a code improvement requires design input, prompt the human. If you were triggered by cron, you have to complete whichever
   job you choose without human input, so skip tasks that aren't no-brainers.

6. If you find an improvement: make changes, run tests and linting, commit with
   a message prefixed with `[code improver]`.

7. Do not feel obligated to make an edit. If the code is already good, just commit:
   `git commit --allow-empty -m "[code improver] no improvements needed"`
   and stop.
```

**Step 2: Delete the old skill directory**

```bash
rm -rf container/skills/code-improver/
```

**Step 3: Update any tests that reference `code-improver` as a skill**

In `tests/test_container_runner.py` at line 1385, the test `test_dev_excluded_when_not_listed` uses `"code-improver"` as an example dev skill name. This is still valid — the test is about the filtering logic, not the existence of the skill. No test change needed.

**Step 4: Commit**

```bash
git add directives/pynchy-code-improver.md
git rm -r container/skills/code-improver/
git commit -m "refactor: convert code-improver skill to pynchy-code-improver directive

The code-improver workflow is specific to the pynchy repo, not a
generic skill. Moving it to a directive scoped to crypdick/pynchy
so it only appears in workspaces with repo_access to the pynchy repo."
```

---

### Task 5: Update config.toml on pynchy-server

**Files:**
- Modify: `~/src/PERSONAL/pynchy/config.toml` on pynchy-server (via SSH)

**Step 1: Add the pynchy-code-improver directive**

Add after the existing `[directives.admin-ops]` block:

```toml
[directives.pynchy-code-improver]
file = "directives/pynchy-code-improver.md"
scope = "crypdick/pynchy"
```

**Step 2: Add skills to admin workspaces**

Add `skills = ["core", "ops"]` to both admin sandbox blocks:

```toml
[sandbox.admin-1]
chat = "connection.slack.synapse.chat.admin-1"
is_admin = true
idle_terminate = false
trigger = "always"
repo_access = "crypdick/pynchy"
skills = ["core", "ops"]

[sandbox.admin-2]
chat = "connection.slack.synapse.chat.admin-2"
is_admin = true
idle_terminate = false
trigger = "always"
repo_access = "crypdick/pynchy"
skills = ["core", "ops"]
```

All other workspaces remain unchanged — they'll inherit the new core-only default.

**Step 3: Verify the config change triggers a restart**

Wait ~30-90s, then check:
```bash
ssh pynchy-server 'curl -s http://localhost:8484/status | python3 -m json.tool'
```

Note: The config change on pynchy-server will take effect after the code changes are deployed (since the code change to `_is_skill_selected` must land first). The directive file and skill deletion are repo changes that deploy via git push → auto-deploy.

---

### Task 6: Deploy and verify

**Step 1: Push code changes**

The git push to main triggers auto-deploy on pynchy-server (pulls, restarts).

**Step 2: Verify deployment**

```bash
ssh pynchy-server 'curl -s http://localhost:8484/status | python3 -m json.tool'
```

Check the service restarted cleanly. Then verify skills by checking journal logs for skill sync messages:

```bash
ssh pynchy-server 'journalctl --user -u pynchy --grep "Skipping skill" -n 20'
```

This should show non-core skills being skipped for most workspaces.
