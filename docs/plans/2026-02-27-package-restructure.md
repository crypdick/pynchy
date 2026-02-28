# Package Restructure — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize `src/pynchy/` into explicit architectural layers: `host/orchestrator/`, `host/container_manager/`, `plugins/`, `config/`, `state/`, and `agent/`.

**Architecture:** Move existing modules into a layered package structure that makes separation of concerns explicit. Each task moves one coherent group of files, updates all imports, and verifies with the existing test suite. No behavior changes — purely structural. Compatibility shims are NOT used; all imports are updated atomically per task.

**Tech Stack:** Python, git, grep (for import updates), pytest

**Branch:** Create a dedicated branch `refactor/package-restructure` before starting.

---

## Target Structure

```
src/pynchy/
├── host/
│   ├── orchestrator/
│   │   ├── app.py                  # composition root
│   │   ├── lifecycle.py            # startup/shutdown phases (was _lifecycle.py)
│   │   ├── agent_runner.py
│   │   ├── concurrency.py          # per-group queue (was group_queue.py)
│   │   ├── session_handler.py
│   │   ├── task_scheduler.py
│   │   ├── adapters.py             # protocol implementations
│   │   ├── dep_factory.py          # DI wiring
│   │   ├── startup_handler.py
│   │   ├── deploy.py
│   │   ├── status.py
│   │   ├── http_server.py
│   │   ├── todos.py
│   │   ├── service_installer.py
│   │   ├── workspace_config.py
│   │   └── messaging/
│   │       ├── inbound.py          # was chat/_message_routing.py
│   │       ├── pipeline.py         # was chat/message_handler.py
│   │       ├── router.py           # was chat/output_handler.py
│   │       ├── streaming.py        # was chat/_streaming.py
│   │       ├── sender.py           # was chat/bus.py
│   │       ├── formatter.py        # was chat/router.py
│   │       ├── commands.py
│   │       ├── reconciler.py
│   │       ├── approval_handler.py
│   │       ├── pending_questions.py
│   │       ├── ask_user_handler.py
│   │       ├── channel_handler.py
│   │       └── reaction_handler.py
│   │
│   └── container_manager/
│       ├── orchestrator.py         # was container_runner/_orchestrator.py
│       ├── session.py              # was container_runner/_session.py
│       ├── session_prep.py         # was container_runner/_session_prep.py
│       ├── mounts.py               # was container_runner/_mounts.py
│       ├── credentials.py          # was container_runner/_credentials.py
│       ├── process.py              # was container_runner/_process.py
│       ├── serialization.py        # was container_runner/_serialization.py
│       ├── snapshots.py            # was container_runner/_snapshots.py
│       ├── docker.py               # was container_runner/_docker.py
│       ├── gateway.py              # was container_runner/gateway.py
│       ├── gateway_builtin.py      # was container_runner/_gateway_builtin.py
│       ├── gateway_litellm.py      # was container_runner/_gateway_litellm.py
│       ├── mcp/
│       │   ├── manager.py          # was container_runner/mcp_manager.py
│       │   ├── lifecycle.py        # was container_runner/_mcp_lifecycle.py
│       │   ├── litellm.py          # was container_runner/_mcp_litellm.py
│       │   └── proxy.py            # was container_runner/_mcp_proxy.py
│       ├── security/               # was security/
│       │   ├── gate.py
│       │   ├── middleware.py
│       │   ├── cop.py
│       │   ├── cop_gate.py
│       │   ├── fencing.py
│       │   ├── approval.py
│       │   ├── audit.py
│       │   ├── mount_security.py
│       │   └── secrets_scanner.py
│       └── ipc/                    # was ipc/
│           ├── watcher.py          # was _watcher.py
│           ├── write.py            # was _write.py
│           ├── registry.py         # was _registry.py
│           ├── protocol.py         # was _protocol.py
│           ├── deps.py             # was _deps.py
│           ├── handlers_approval.py
│           ├── handlers_ask_user.py
│           ├── handlers_deploy.py
│           ├── handlers_groups.py
│           ├── handlers_lifecycle.py
│           ├── handlers_security.py
│           ├── handlers_service.py
│           └── handlers_tasks.py
│
├── plugins/
│   ├── registry.py                 # was plugin/__init__.py
│   ├── hookspecs.py                # was plugin/hookspecs.py
│   ├── channel_runtime.py          # was chat/channel_runtime.py
│   ├── channels/
│   │   ├── slack/
│   │   ├── whatsapp/
│   │   └── tui/
│   ├── agent_cores/
│   │   ├── claude.py
│   │   └── openai.py
│   ├── integrations/
│   │   ├── browser.py
│   │   ├── caldav.py
│   │   ├── google_setup.py
│   │   ├── notebook_server/
│   │   ├── playwright_browser.py
│   │   ├── slack_token_extractor.py
│   │   └── x_integration.py
│   ├── observers/
│   │   └── sqlite_observer/
│   ├── memory/
│   │   └── sqlite_memory/
│   ├── tunnels/
│   │   └── tailscale.py
│   └── runtimes/
│       ├── detection.py            # was runtime/runtime.py
│       ├── system_checks.py        # was runtime/system_checks.py
│       ├── apple_runtime/
│       └── docker_runtime/
│
├── agent/                          # container-side code (from repo-root container/)
│
├── config/
│   ├── settings.py                 # was config.py
│   ├── models.py                   # was config_models.py
│   ├── access.py                   # was config_access.py
│   ├── mcp.py                      # was config_mcp.py
│   ├── refs.py                     # was config_refs.py
│   └── directives.py               # was directives.py
│
├── state/                          # was db/
│   ├── connection.py               # was _connection.py
│   ├── schema.py                   # was _schema.py
│   ├── aliases.py
│   ├── channel_cursors.py
│   ├── chats.py
│   ├── events.py
│   ├── groups.py
│   ├── host_jobs.py
│   ├── messages.py
│   ├── outbound.py
│   ├── sessions.py
│   └── tasks.py
│
├── git_ops/                        # stays (can't use git/ — name collision)
│
│   # Ambient / shared (no pynchy deps beyond logger)
├── __init__.py
├── __main__.py
├── types.py
├── logger.py
├── event_bus.py
└── utils.py
```

---

## Migration Strategy

Each task:
1. `git mv` files to their new locations
2. Create `__init__.py` files for new packages with appropriate re-exports
3. `grep -rn "old_import_path" src/ tests/` to find all imports to update
4. Update all import statements (both `from X import Y` and `import X`)
5. Check `TYPE_CHECKING` blocks — they contain imports too
6. Check lazy imports inside functions (grep for the old path in string form)
7. Run: `uvx pytest -x` — stop on first failure to catch issues early
8. Run: `grep -rn "old_path" src/ tests/` — verify no stale references remain
9. Commit

**Important patterns in this codebase:**
- Heavy use of lazy imports inside methods (especially in `adapters.py`, `dep_factory.py`, `startup_handler.py`, IPC handlers)
- `TYPE_CHECKING`-guarded imports (especially `app.py` ↔ `_lifecycle.py` cycle, `dep_factory.py`)
- `__init__.py` re-export facades (`db/__init__.py` is the fattest — re-exports ~50 symbols)
- `plugin/__init__.py` contains real implementation, not just re-exports

**Stripping leading underscores:** Files like `_lifecycle.py`, `_message_routing.py`, `_streaming.py`, `_watcher.py` etc. lose their underscore prefix when moved — the underscore was a "private to this package" signal that becomes unnecessary when the file sits in a properly named package.

---

### Task 1: Create `config/` package

**Files:**

| From | To |
|------|-----|
| `src/pynchy/config.py` | `src/pynchy/config/settings.py` |
| `src/pynchy/config_models.py` | `src/pynchy/config/models.py` |
| `src/pynchy/config_access.py` | `src/pynchy/config/access.py` |
| `src/pynchy/config_mcp.py` | `src/pynchy/config/mcp.py` |
| `src/pynchy/config_refs.py` | `src/pynchy/config/refs.py` |
| `src/pynchy/directives.py` | `src/pynchy/config/directives.py` |

**Note:** `config.py` → `config/` is a file-to-package conversion. You must remove the file before creating the directory.

**Step 1: Move files**

```bash
cd src/pynchy
git mv config.py config_tmp.py  # temp rename to free the name
mkdir -p config
git mv config_tmp.py config/settings.py
git mv config_models.py config/models.py
git mv config_access.py config/access.py
git mv config_mcp.py config/mcp.py
git mv config_refs.py config/refs.py
git mv directives.py config/directives.py
```

**Step 2: Create `config/__init__.py`**

Re-export everything that was previously importable from the old paths. This is critical because `from pynchy.config import get_settings` is used in ~30 files.

```python
"""Configuration — settings, models, access resolution, directives."""

# Re-export the main settings interface so `from pynchy.config import get_settings`
# continues to work. Callers should migrate to `from pynchy.config.settings import ...`
# over time.
from pynchy.config.settings import *  # noqa: F401,F403
```

This means `from pynchy.config import get_settings` still works without updating every file immediately.

**Step 3: Update internal cross-references**

These files import from each other and need path updates:

- `config/settings.py` imports from `config_models` and `config_mcp` → update to `config.models` and `config.mcp`
- `config/models.py` — check for any cross-imports
- `config/access.py` imports from `config_models` → update to `config.models`
- `config/refs.py` imports from `config_models` → update to `config.models`
- `config/directives.py` imports from `config` → update to `config.settings`

**Step 4: Update external imports**

Grep and update:

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.config_models import` | `from pynchy.config.models import` |
| `from pynchy.config_access import` | `from pynchy.config.access import` |
| `from pynchy.config_mcp import` | `from pynchy.config.mcp import` |
| `from pynchy.config_refs import` | `from pynchy.config.refs import` |
| `from pynchy.directives import` | `from pynchy.config.directives import` |
| `import pynchy.config_models` | `import pynchy.config.models` |

**Do NOT update** `from pynchy.config import` — the `__init__.py` re-export handles this.

**Step 5: Verify and commit**

```bash
grep -rn "from pynchy.config_models\|from pynchy.config_access\|from pynchy.config_mcp\|from pynchy.config_refs\|from pynchy.directives import\|import pynchy.config_models\|import pynchy.directives" src/ tests/
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: consolidate config files into config/ package"
```

---

### Task 2: Rename `db/` → `state/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/db/` (entire directory) | `src/pynchy/state/` |

All internal filenames stay the same, just strip leading underscores:

| From | To |
|------|-----|
| `db/_connection.py` | `state/connection.py` |
| `db/_schema.py` | `state/schema.py` |
| `db/*.py` (rest) | `state/*.py` (same names) |

**Step 1: Move**

```bash
cd src/pynchy
git mv db state
cd state
git mv _connection.py connection.py
git mv _schema.py schema.py
```

**Step 2: Update `state/__init__.py`**

Update internal imports within the file — it re-exports from submodules. Change:
- `from pynchy.db._connection import` → `from pynchy.state.connection import`
- `from pynchy.db._schema import` → `from pynchy.state.schema import`
- `from pynchy.db.X import` → `from pynchy.state.X import`

**Step 3: Update all external imports**

This is the highest-impact change — `pynchy.db` is imported by ~20 files.

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.db import` | `from pynchy.state import` |
| `from pynchy.db.` | `from pynchy.state.` |
| `import pynchy.db` | `import pynchy.state` |
| `pynchy.db.` (in strings, e.g. test mocks) | `pynchy.state.` |

Files that import from `pynchy.db` (non-exhaustive — grep to find all):
- `adapters.py`, `deploy.py`, `startup_handler.py`, `status.py`, `_lifecycle.py`, `app.py`
- `workspace_config.py`, `http_server.py`
- `chat/message_handler.py`, `chat/output_handler.py`, `chat/_message_routing.py`, `chat/reconciler.py`, `chat/bus.py`
- `ipc/_handlers_*.py` (multiple)
- `container_runner/_session.py`
- `observers/plugins/sqlite_observer/observer.py`
- `memory/plugins/sqlite_memory/backend.py`
- Many test files: `tests/test_db.py`, `tests/conftest.py`, etc.

**Step 4: Update cross-references within state/**

Submodules that import from sibling submodules (e.g., `from pynchy.db._connection import` inside `messages.py`) need updating.

**Step 5: Verify and commit**

```bash
grep -rn "pynchy\.db[^a-z_]" src/ tests/  # finds pynchy.db but not pynchy.db_something
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: rename db/ to state/"
```

---

### Task 3: Create `plugins/` package (registry + hookspecs)

**Files:**

| From | To |
|------|-----|
| `src/pynchy/plugin/__init__.py` | `src/pynchy/plugins/registry.py` |
| `src/pynchy/plugin/hookspecs.py` | `src/pynchy/plugins/hookspecs.py` |

**Step 1: Move**

```bash
cd src/pynchy
mkdir -p plugins
git mv plugin/hookspecs.py plugins/hookspecs.py
# plugin/__init__.py contains real implementation — move to registry.py
cp plugin/__init__.py plugins/registry.py
git rm -r plugin/
git add plugins/registry.py
```

**Step 2: Create `plugins/__init__.py`**

```python
"""Plugin system — registry, hookspecs, and all plugin implementations."""

from pynchy.plugins.registry import get_plugin_manager, collect_hook_results  # noqa: F401
```

**Step 3: Update `plugins/registry.py` internal imports**

- `from pynchy.plugin.hookspecs import` → `from pynchy.plugins.hookspecs import`
- Any `importlib.import_module("pynchy.X.plugins.Y")` calls — these are how built-in plugins are discovered. They will need updating as plugins move in later tasks. For now, keep them pointing to the old paths (the plugin modules haven't moved yet).

**Step 4: Update external imports**

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.plugin import` | `from pynchy.plugins import` |
| `from pynchy.plugin.hookspecs import` | `from pynchy.plugins.hookspecs import` |
| `import pynchy.plugin` | `import pynchy.plugins` |

Key importers:
- `_lifecycle.py` — `from pynchy.plugin import get_plugin_manager`
- Plugin implementation files that import hookspecs (e.g., `hookimpl`)

**Step 5: Verify and commit**

```bash
grep -rn "pynchy\.plugin[^s]" src/ tests/  # finds pynchy.plugin but not pynchy.plugins
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: create plugins/ package with registry and hookspecs"
```

---

### Task 4: Move channel plugins → `plugins/channels/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/chat/plugins/slack/` | `src/pynchy/plugins/channels/slack/` |
| `src/pynchy/chat/plugins/whatsapp/` | `src/pynchy/plugins/channels/whatsapp/` |
| `src/pynchy/chat/plugins/tui/` | `src/pynchy/plugins/channels/tui/` |
| `src/pynchy/chat/channel_runtime.py` | `src/pynchy/plugins/channel_runtime.py` |

**Step 1: Move**

```bash
cd src/pynchy
mkdir -p plugins/channels
git mv chat/plugins/slack plugins/channels/slack
git mv chat/plugins/whatsapp plugins/channels/whatsapp
git mv chat/plugins/tui plugins/channels/tui
git mv chat/channel_runtime.py plugins/channel_runtime.py
# Clean up empty chat/plugins/ directory
git rm chat/plugins/__init__.py
rmdir chat/plugins
```

**Step 2: Create `plugins/channels/__init__.py`**

```python
"""Channel plugins — Slack, WhatsApp, TUI."""
```

**Step 3: Update imports**

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.chat.plugins.slack` | `from pynchy.plugins.channels.slack` |
| `from pynchy.chat.plugins.whatsapp` | `from pynchy.plugins.channels.whatsapp` |
| `from pynchy.chat.plugins.tui` | `from pynchy.plugins.channels.tui` |
| `from pynchy.chat.channel_runtime import` | `from pynchy.plugins.channel_runtime import` |
| `pynchy.chat.plugins.` (in importlib strings) | `pynchy.plugins.channels.` |

Also update `plugins/registry.py` — it uses `importlib.import_module` to discover built-in channel plugins. Update those strings.

**Step 4: Update internal imports within moved files**

Channel plugin files may import from `pynchy.chat.bus`, `pynchy.types`, `pynchy.config`, etc. These haven't moved yet (except config in Task 1), so check each moved file for imports that need updating.

**Step 5: Verify and commit**

```bash
grep -rn "pynchy\.chat\.plugins\|pynchy\.chat\.channel_runtime" src/ tests/
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: move channel plugins to plugins/channels/"
```

---

### Task 5: Move remaining plugin packages → `plugins/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/integrations/browser.py` | `src/pynchy/plugins/integrations/browser.py` |
| `src/pynchy/integrations/plugins/*.py` | `src/pynchy/plugins/integrations/*.py` |
| `src/pynchy/integrations/plugins/notebook_server/` | `src/pynchy/plugins/integrations/notebook_server/` |
| `src/pynchy/observers/plugins/sqlite_observer/` | `src/pynchy/plugins/observers/sqlite_observer/` |
| `src/pynchy/memory/plugins/sqlite_memory/` | `src/pynchy/plugins/memory/sqlite_memory/` |
| `src/pynchy/tunnels/plugins/tailscale.py` | `src/pynchy/plugins/tunnels/tailscale.py` |
| `src/pynchy/agent_framework/plugins/claude.py` | `src/pynchy/plugins/agent_cores/claude.py` |
| `src/pynchy/agent_framework/plugins/openai.py` | `src/pynchy/plugins/agent_cores/openai.py` |
| `src/pynchy/runtime/runtime.py` | `src/pynchy/plugins/runtimes/detection.py` |
| `src/pynchy/runtime/system_checks.py` | `src/pynchy/plugins/runtimes/system_checks.py` |
| `src/pynchy/runtime/plugins/apple_runtime/` | `src/pynchy/plugins/runtimes/apple_runtime/` |
| `src/pynchy/runtime/plugins/docker_runtime/` | `src/pynchy/plugins/runtimes/docker_runtime/` |

**Step 1: Move all files**

```bash
cd src/pynchy

# Integrations — flatten plugins/ subdirectory
mkdir -p plugins/integrations
git mv integrations/browser.py plugins/integrations/browser.py
git mv integrations/plugins/caldav.py plugins/integrations/caldav.py
git mv integrations/plugins/google_setup.py plugins/integrations/google_setup.py
git mv integrations/plugins/notebook_server plugins/integrations/notebook_server
git mv integrations/plugins/playwright_browser.py plugins/integrations/playwright_browser.py
git mv integrations/plugins/slack_token_extractor.py plugins/integrations/slack_token_extractor.py
git mv integrations/plugins/x_integration.py plugins/integrations/x_integration.py
git rm -r integrations/

# Observers
mkdir -p plugins/observers
git mv observers/plugins/sqlite_observer plugins/observers/sqlite_observer
git rm -r observers/

# Memory
mkdir -p plugins/memory
git mv memory/plugins/sqlite_memory plugins/memory/sqlite_memory
git rm -r memory/

# Tunnels
mkdir -p plugins/tunnels
git mv tunnels/plugins/tailscale.py plugins/tunnels/tailscale.py
git rm -r tunnels/

# Agent cores
mkdir -p plugins/agent_cores
git mv agent_framework/plugins/claude.py plugins/agent_cores/claude.py
git mv agent_framework/plugins/openai.py plugins/agent_cores/openai.py
git rm -r agent_framework/

# Runtimes
mkdir -p plugins/runtimes
git mv runtime/runtime.py plugins/runtimes/detection.py
git mv runtime/system_checks.py plugins/runtimes/system_checks.py
git mv runtime/plugins/apple_runtime plugins/runtimes/apple_runtime
git mv runtime/plugins/docker_runtime plugins/runtimes/docker_runtime
git rm -r runtime/
```

**Step 2: Create `__init__.py` files for each new subpackage**

Minimal package markers. Any re-exports that the old `__init__.py` files provided should be replicated (check each old `__init__.py` before deleting).

**Step 3: Update `plugins/registry.py`**

The registry uses `importlib.import_module()` strings to discover built-in plugins. Update ALL of these to the new paths.

**Step 4: Update external imports**

Major patterns:

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.integrations` | `from pynchy.plugins.integrations` |
| `from pynchy.observers` | `from pynchy.plugins.observers` |
| `from pynchy.memory` | `from pynchy.plugins.memory` |
| `from pynchy.tunnels` | `from pynchy.plugins.tunnels` |
| `from pynchy.agent_framework` | `from pynchy.plugins.agent_cores` |
| `from pynchy.runtime.runtime import` | `from pynchy.plugins.runtimes.detection import` |
| `from pynchy.runtime.system_checks import` | `from pynchy.plugins.runtimes.system_checks import` |
| `from pynchy.runtime import` | Check what was re-exported, update accordingly |

Key importers of runtime:
- `_lifecycle.py` — system checks
- `__main__.py` — `get_runtime()`
- `container_runner/_orchestrator.py` — `get_runtime()`

**Step 5: Verify and commit**

```bash
grep -rn "pynchy\.integrations\|pynchy\.observers[^/]\|pynchy\.memory\|pynchy\.tunnels\|pynchy\.agent_framework\|pynchy\.runtime" src/ tests/
# Should return nothing (except pynchy.plugins.runtimes references)

uvx pytest -x
git add -A && git commit -m "refactor: consolidate all plugin packages under plugins/"
```

---

### Task 6: Create `host/container_manager/`

This is the biggest structural move. We're combining `container_runner/`, `security/`, and `ipc/` into one package.

**Files:**

| From | To |
|------|-----|
| `src/pynchy/container_runner/_orchestrator.py` | `src/pynchy/host/container_manager/orchestrator.py` |
| `src/pynchy/container_runner/_session.py` | `src/pynchy/host/container_manager/session.py` |
| `src/pynchy/container_runner/_session_prep.py` | `src/pynchy/host/container_manager/session_prep.py` |
| `src/pynchy/container_runner/_mounts.py` | `src/pynchy/host/container_manager/mounts.py` |
| `src/pynchy/container_runner/_credentials.py` | `src/pynchy/host/container_manager/credentials.py` |
| `src/pynchy/container_runner/_process.py` | `src/pynchy/host/container_manager/process.py` |
| `src/pynchy/container_runner/_serialization.py` | `src/pynchy/host/container_manager/serialization.py` |
| `src/pynchy/container_runner/_snapshots.py` | `src/pynchy/host/container_manager/snapshots.py` |
| `src/pynchy/container_runner/_docker.py` | `src/pynchy/host/container_manager/docker.py` |
| `src/pynchy/container_runner/gateway.py` | `src/pynchy/host/container_manager/gateway.py` |
| `src/pynchy/container_runner/_gateway_builtin.py` | `src/pynchy/host/container_manager/gateway_builtin.py` |
| `src/pynchy/container_runner/_gateway_litellm.py` | `src/pynchy/host/container_manager/gateway_litellm.py` |
| `src/pynchy/container_runner/mcp_manager.py` | `src/pynchy/host/container_manager/mcp/manager.py` |
| `src/pynchy/container_runner/_mcp_lifecycle.py` | `src/pynchy/host/container_manager/mcp/lifecycle.py` |
| `src/pynchy/container_runner/_mcp_litellm.py` | `src/pynchy/host/container_manager/mcp/litellm.py` |
| `src/pynchy/container_runner/_mcp_proxy.py` | `src/pynchy/host/container_manager/mcp/proxy.py` |
| `src/pynchy/security/*` | `src/pynchy/host/container_manager/security/*` (same filenames) |
| `src/pynchy/ipc/_watcher.py` | `src/pynchy/host/container_manager/ipc/watcher.py` |
| `src/pynchy/ipc/_write.py` | `src/pynchy/host/container_manager/ipc/write.py` |
| `src/pynchy/ipc/_registry.py` | `src/pynchy/host/container_manager/ipc/registry.py` |
| `src/pynchy/ipc/_protocol.py` | `src/pynchy/host/container_manager/ipc/protocol.py` |
| `src/pynchy/ipc/_deps.py` | `src/pynchy/host/container_manager/ipc/deps.py` |
| `src/pynchy/ipc/_handlers_*.py` | `src/pynchy/host/container_manager/ipc/handlers_*.py` |

**Step 1: Create directory structure**

```bash
cd src/pynchy
mkdir -p host/container_manager/mcp
mkdir -p host/container_manager/security
mkdir -p host/container_manager/ipc
```

**Step 2: Move container_runner/ files**

```bash
# Main container_runner files (strip underscores)
for f in _orchestrator _session _session_prep _mounts _credentials _process _serialization _snapshots _docker _gateway_builtin _gateway_litellm; do
    git mv container_runner/${f}.py host/container_manager/${f#_}.py
done
git mv container_runner/gateway.py host/container_manager/gateway.py

# MCP files
git mv container_runner/mcp_manager.py host/container_manager/mcp/manager.py
for f in _mcp_lifecycle _mcp_litellm _mcp_proxy; do
    name=${f#_mcp_}  # strip _mcp_ prefix
    git mv container_runner/${f}.py host/container_manager/mcp/${name}.py
done

git rm container_runner/__init__.py
rmdir container_runner
```

**Step 3: Move security/ and ipc/**

```bash
# Security — files keep their names
for f in gate middleware cop cop_gate fencing approval audit mount_security secrets_scanner; do
    git mv security/${f}.py host/container_manager/security/${f}.py
done
git rm security/__init__.py
rmdir security

# IPC — strip underscores
for f in _watcher _write _registry _protocol _deps; do
    git mv ipc/${f}.py host/container_manager/ipc/${f#_}.py
done
for f in _handlers_approval _handlers_ask_user _handlers_deploy _handlers_groups _handlers_lifecycle _handlers_security _handlers_service _handlers_tasks; do
    git mv ipc/${f}.py host/container_manager/ipc/${f#_}.py
done
git rm ipc/__init__.py
rmdir ipc
```

**Step 4: Create `__init__.py` files**

`host/__init__.py`:
```python
"""Host-side pynchy code — orchestration and container management."""
```

`host/container_manager/__init__.py`:
Replicate the re-exports from the old `container_runner/__init__.py`. Key exports: `ContainerSession`, `SessionDiedError`, `create_session`, `destroy_all_sessions`, `destroy_session`, `get_session`, `get_session_output_handler`, `OnOutput`, `has_api_credentials`, `resolve_agent_core`, `resolve_container_timeout`, `write_groups_snapshot`, `write_tasks_snapshot`.

Update the import sources to point to the new submodule locations.

`host/container_manager/mcp/__init__.py`:
```python
"""MCP server lifecycle — Docker management, LiteLLM sync, proxy."""
```

`host/container_manager/security/__init__.py`:
Replicate re-exports from old `security/__init__.py`: `PolicyDecision`, `PolicyDeniedError`, `SecurityPolicy`, `prune_security_audit`, `record_security_event`. Update sources.

`host/container_manager/ipc/__init__.py`:
Replicate re-exports from old `ipc/__init__.py`: `IpcDeps`, `dispatch`, `start_ipc_watcher`. Also import all `handlers_*` modules to trigger self-registration. Update sources.

**Step 5: Update ALL imports**

This is the largest import update. Major patterns:

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.container_runner` | `from pynchy.host.container_manager` |
| `from pynchy.container_runner._X import` | `from pynchy.host.container_manager.X import` |
| `from pynchy.container_runner.mcp_manager import` | `from pynchy.host.container_manager.mcp.manager import` |
| `from pynchy.container_runner._mcp_X import` | `from pynchy.host.container_manager.mcp.X import` |
| `from pynchy.security` | `from pynchy.host.container_manager.security` |
| `from pynchy.security.X import` | `from pynchy.host.container_manager.security.X import` |
| `from pynchy.ipc` | `from pynchy.host.container_manager.ipc` |
| `from pynchy.ipc._X import` | `from pynchy.host.container_manager.ipc.X import` |

**Warning:** The IPC handler files import heavily from each other and from other pynchy modules. Check each handler file's imports carefully.

**Warning:** `security/approval.py` is deliberately excluded from `security/__init__.py` to avoid circular imports (noted in the old `__init__.py` docstring). Maintain this pattern.

**Step 6: Verify and commit**

```bash
grep -rn "pynchy\.container_runner\|pynchy\.security[^/]\|pynchy\.ipc[^/]" src/ tests/
# Should return nothing (pynchy.host.container_manager.security is fine)

uvx pytest -x
git add -A && git commit -m "refactor: create host/container_manager/ from container_runner + security + ipc"
```

---

### Task 7: Create `host/orchestrator/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/app.py` | `src/pynchy/host/orchestrator/app.py` |
| `src/pynchy/_lifecycle.py` | `src/pynchy/host/orchestrator/lifecycle.py` |
| `src/pynchy/agent_runner.py` | `src/pynchy/host/orchestrator/agent_runner.py` |
| `src/pynchy/group_queue.py` | `src/pynchy/host/orchestrator/concurrency.py` |
| `src/pynchy/session_handler.py` | `src/pynchy/host/orchestrator/session_handler.py` |
| `src/pynchy/task_scheduler.py` | `src/pynchy/host/orchestrator/task_scheduler.py` |
| `src/pynchy/adapters.py` | `src/pynchy/host/orchestrator/adapters.py` |
| `src/pynchy/dep_factory.py` | `src/pynchy/host/orchestrator/dep_factory.py` |
| `src/pynchy/startup_handler.py` | `src/pynchy/host/orchestrator/startup_handler.py` |
| `src/pynchy/deploy.py` | `src/pynchy/host/orchestrator/deploy.py` |
| `src/pynchy/status.py` | `src/pynchy/host/orchestrator/status.py` |
| `src/pynchy/http_server.py` | `src/pynchy/host/orchestrator/http_server.py` |
| `src/pynchy/todos.py` | `src/pynchy/host/orchestrator/todos.py` |
| `src/pynchy/service_installer.py` | `src/pynchy/host/orchestrator/service_installer.py` |
| `src/pynchy/workspace_config.py` | `src/pynchy/host/orchestrator/workspace_config.py` |

**Step 1: Move files**

```bash
cd src/pynchy
mkdir -p host/orchestrator
git mv app.py host/orchestrator/app.py
git mv _lifecycle.py host/orchestrator/lifecycle.py
git mv agent_runner.py host/orchestrator/agent_runner.py
git mv group_queue.py host/orchestrator/concurrency.py
git mv session_handler.py host/orchestrator/session_handler.py
git mv task_scheduler.py host/orchestrator/task_scheduler.py
git mv adapters.py host/orchestrator/adapters.py
git mv dep_factory.py host/orchestrator/dep_factory.py
git mv startup_handler.py host/orchestrator/startup_handler.py
git mv deploy.py host/orchestrator/deploy.py
git mv status.py host/orchestrator/status.py
git mv http_server.py host/orchestrator/http_server.py
git mv todos.py host/orchestrator/todos.py
git mv service_installer.py host/orchestrator/service_installer.py
git mv workspace_config.py host/orchestrator/workspace_config.py
```

**Step 2: Create `host/orchestrator/__init__.py`**

```python
"""Orchestrator — app lifecycle, agent execution, scheduling, messaging."""
```

**Step 3: Update `__main__.py`**

This is the entry point — it must point to the new app location:

```python
# Old:
from pynchy.app import PynchyApp
# New:
from pynchy.host.orchestrator.app import PynchyApp
```

Also update the `get_runtime` import (already moved in Task 5) and any other lazy imports.

**Step 4: Update all imports**

Major patterns:

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.app import` | `from pynchy.host.orchestrator.app import` |
| `from pynchy._lifecycle import` | `from pynchy.host.orchestrator.lifecycle import` |
| `from pynchy import _lifecycle` | `from pynchy.host.orchestrator import lifecycle` |
| `from pynchy.agent_runner import` | `from pynchy.host.orchestrator.agent_runner import` |
| `from pynchy.group_queue import` | `from pynchy.host.orchestrator.concurrency import` |
| `from pynchy.session_handler import` | `from pynchy.host.orchestrator.session_handler import` |
| `from pynchy.task_scheduler import` | `from pynchy.host.orchestrator.task_scheduler import` |
| `from pynchy.adapters import` | `from pynchy.host.orchestrator.adapters import` |
| `from pynchy.dep_factory import` | `from pynchy.host.orchestrator.dep_factory import` |
| `from pynchy.startup_handler import` | `from pynchy.host.orchestrator.startup_handler import` |
| `from pynchy import startup_handler` | `from pynchy.host.orchestrator import startup_handler` |
| `from pynchy.deploy import` | `from pynchy.host.orchestrator.deploy import` |
| `from pynchy.status import` | `from pynchy.host.orchestrator.status import` |
| `from pynchy.http_server import` | `from pynchy.host.orchestrator.http_server import` |
| `from pynchy.todos import` | `from pynchy.host.orchestrator.todos import` |
| `from pynchy.service_installer import` | `from pynchy.host.orchestrator.service_installer import` |
| `from pynchy.workspace_config import` | `from pynchy.host.orchestrator.workspace_config import` |

**Critical:** The `app.py` ↔ `_lifecycle.py` circular import uses `TYPE_CHECKING` guards. Both files reference each other. Make sure the updated TYPE_CHECKING imports point to the new paths.

**Critical:** `dep_factory.py` has TYPE_CHECKING imports for Protocols from multiple subsystems. These were already updated in Task 6 (ipc, container_manager) but double-check.

**Warning:** `group_queue.py` → `concurrency.py` is a rename, not just a move. Grep for both the module path AND the old name in case anything references it by string.

**Step 5: Verify and commit**

```bash
grep -rn "from pynchy\.app import\|from pynchy\._lifecycle\|from pynchy\.agent_runner\|from pynchy\.group_queue\|from pynchy\.session_handler import\|from pynchy\.task_scheduler\|from pynchy\.adapters\|from pynchy\.dep_factory\|from pynchy\.startup_handler\|from pynchy\.deploy\|from pynchy\.status import\|from pynchy\.http_server\|from pynchy\.todos\|from pynchy\.service_installer\|from pynchy\.workspace_config\|from pynchy import startup_handler\|from pynchy import _lifecycle" src/ tests/
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: create host/orchestrator/ with all orchestration modules"
```

---

### Task 8: Dissolve `chat/` into `host/orchestrator/messaging/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/chat/_message_routing.py` | `src/pynchy/host/orchestrator/messaging/inbound.py` |
| `src/pynchy/chat/message_handler.py` | `src/pynchy/host/orchestrator/messaging/pipeline.py` |
| `src/pynchy/chat/output_handler.py` | `src/pynchy/host/orchestrator/messaging/router.py` |
| `src/pynchy/chat/_streaming.py` | `src/pynchy/host/orchestrator/messaging/streaming.py` |
| `src/pynchy/chat/bus.py` | `src/pynchy/host/orchestrator/messaging/sender.py` |
| `src/pynchy/chat/router.py` | `src/pynchy/host/orchestrator/messaging/formatter.py` |
| `src/pynchy/chat/commands.py` | `src/pynchy/host/orchestrator/messaging/commands.py` |
| `src/pynchy/chat/reconciler.py` | `src/pynchy/host/orchestrator/messaging/reconciler.py` |
| `src/pynchy/chat/approval_handler.py` | `src/pynchy/host/orchestrator/messaging/approval_handler.py` |
| `src/pynchy/chat/pending_questions.py` | `src/pynchy/host/orchestrator/messaging/pending_questions.py` |
| `src/pynchy/chat/ask_user_handler.py` | `src/pynchy/host/orchestrator/messaging/ask_user_handler.py` |
| `src/pynchy/chat/channel_handler.py` | `src/pynchy/host/orchestrator/messaging/channel_handler.py` |
| `src/pynchy/chat/reaction_handler.py` | `src/pynchy/host/orchestrator/messaging/reaction_handler.py` |

**Step 1: Move files**

```bash
cd src/pynchy
mkdir -p host/orchestrator/messaging

git mv chat/_message_routing.py host/orchestrator/messaging/inbound.py
git mv chat/message_handler.py host/orchestrator/messaging/pipeline.py
git mv chat/output_handler.py host/orchestrator/messaging/router.py
git mv chat/_streaming.py host/orchestrator/messaging/streaming.py
git mv chat/bus.py host/orchestrator/messaging/sender.py
git mv chat/router.py host/orchestrator/messaging/formatter.py
git mv chat/commands.py host/orchestrator/messaging/commands.py
git mv chat/reconciler.py host/orchestrator/messaging/reconciler.py
git mv chat/approval_handler.py host/orchestrator/messaging/approval_handler.py
git mv chat/pending_questions.py host/orchestrator/messaging/pending_questions.py
git mv chat/ask_user_handler.py host/orchestrator/messaging/ask_user_handler.py
git mv chat/channel_handler.py host/orchestrator/messaging/channel_handler.py
git mv chat/reaction_handler.py host/orchestrator/messaging/reaction_handler.py

# Remove the now-empty chat/ directory
git rm chat/__init__.py
rmdir chat
```

**Step 2: Create `host/orchestrator/messaging/__init__.py`**

```python
"""Messaging pipeline — inbound routing, processing, and outbound delivery."""
```

**Step 3: Update imports within messaging/ files**

These files import heavily from each other. Update all internal cross-references:

| Old | New |
|-----|-----|
| `from pynchy.chat._message_routing import` | `from pynchy.host.orchestrator.messaging.inbound import` |
| `from pynchy.chat.message_handler import` | `from pynchy.host.orchestrator.messaging.pipeline import` |
| `from pynchy.chat.output_handler import` | `from pynchy.host.orchestrator.messaging.router import` |
| `from pynchy.chat._streaming import` | `from pynchy.host.orchestrator.messaging.streaming import` |
| `from pynchy.chat.bus import` | `from pynchy.host.orchestrator.messaging.sender import` |
| `from pynchy.chat.router import` | `from pynchy.host.orchestrator.messaging.formatter import` |
| `from pynchy.chat.commands import` | `from pynchy.host.orchestrator.messaging.commands import` |
| `from pynchy.chat.reconciler import` | `from pynchy.host.orchestrator.messaging.reconciler import` |
| `from pynchy.chat.approval_handler import` | `from pynchy.host.orchestrator.messaging.approval_handler import` |
| `from pynchy.chat.pending_questions import` | `from pynchy.host.orchestrator.messaging.pending_questions import` |
| `from pynchy.chat.ask_user_handler import` | `from pynchy.host.orchestrator.messaging.ask_user_handler import` |
| `from pynchy.chat.channel_handler import` | `from pynchy.host.orchestrator.messaging.channel_handler import` |
| `from pynchy.chat import channel_handler` | `from pynchy.host.orchestrator.messaging import channel_handler` |

**Step 4: Update external imports**

Other modules that import from `chat/`:
- `host/orchestrator/app.py` — imports `channel_handler`, `ask_user_handler`, `reaction_handler`
- `host/orchestrator/lifecycle.py` — imports from `chat._message_routing` (now `messaging.inbound`)
- `host/orchestrator/dep_factory.py` — may reference chat modules
- `host/container_manager/ipc/handlers_*.py` — some handlers reference `chat.pending_questions`, `chat.bus`
- Test files: `tests/test_output_handler.py`, `tests/test_router.py`, etc.

**Step 5: Verify and commit**

```bash
grep -rn "pynchy\.chat" src/ tests/
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: dissolve chat/ into host/orchestrator/messaging/"
```

---

### Task 9: Move `git_ops/` into `host/`

**Files:**

| From | To |
|------|-----|
| `src/pynchy/git_ops/` (entire directory) | `src/pynchy/host/git_ops/` |

**Step 1: Move**

```bash
cd src/pynchy
git mv git_ops host/git_ops
```

**Step 2: Update imports**

| Old pattern | New pattern |
|------------|------------|
| `from pynchy.git_ops` | `from pynchy.host.git_ops` |
| `import pynchy.git_ops` | `import pynchy.host.git_ops` |

Key importers: `host/orchestrator/agent_runner.py`, `host/orchestrator/startup_handler.py`, `host/orchestrator/status.py`, `host/orchestrator/http_server.py`, `host/orchestrator/dep_factory.py`, `host/orchestrator/lifecycle.py`, `host/container_manager/orchestrator.py`.

**Step 3: Verify and commit**

```bash
grep -rn "pynchy\.git_ops" src/ tests/ | grep -v "pynchy\.host\.git_ops"
# Should return nothing

uvx pytest -x
git add -A && git commit -m "refactor: move git_ops/ into host/"
```

---

### Task 10: Move container-side code → `src/pynchy/agent/`

**Context:** The `container/` directory at the repo root contains the code that runs INSIDE agent containers. It's a separate deployment unit — not a Python package imported by the host. Moving it to `src/pynchy/agent/` is a packaging/build change.

**Files:**

| From | To |
|------|-----|
| `container/` (repo root) | `src/pynchy/agent/` |

**Step 1: Assess impact**

Before moving, check:
- `container/build.sh` — how does the container image get built? What paths does it reference?
- `container/Dockerfile` (if any) — COPY instructions reference the old path
- `host/container_manager/mounts.py` — volume mount paths may reference `container/`
- `host/container_manager/session_prep.py` — copies files from `container/` into IPC dirs
- Any `config.toml` paths referencing `container/`

**Step 2: Move**

```bash
git mv container src/pynchy/agent
```

**Step 3: Update build scripts and mount paths**

Update every reference to `container/` in:
- Build scripts
- Mount construction code (`mounts.py`)
- Session prep code (`session_prep.py`)
- Config files
- CI/CD if any
- `launchd/` plist files if they reference container paths

**Step 4: Update `__main__.py`**

The `build` subcommand references `container/build.sh`. Update the path.

**Step 5: Verify**

```bash
grep -rn "container/" src/ tests/ --include="*.py" | grep -v "container_manager\|pynchy/agent/"
# Check for stale references to the old container/ path

# Build the container image to verify:
# (exact command depends on your build setup)

uvx pytest -x
git add -A && git commit -m "refactor: move container-side code to src/pynchy/agent/"
```

---

### Task 11: Update documentation

**Files to update:**
- `CLAUDE.md` — Key Files table, Quick Context, all file path references
- `README.md` — any architecture references
- `docs/` — architecture docs, plugin authoring guide
- `.claude/skills/` — any skills that reference file paths (pynchy-dev, pynchy-ops, etc.). Use the `docs-manager` skill for guidance.
- `backlog/TODO.md` — file path references in work items
- Existing plan files in `docs/plans/` — leave as-is (they're historical)

**Step 1: Update CLAUDE.md**

Rewrite the "Key Files" table to reflect the new structure. Update the "Quick Context" section.

**Step 2: Update architecture docs**

Update any file path references in `docs/` that point to old locations.

**Step 3: Update skills**

Check `.claude/skills/` for file path references.

**Step 4: Commit**

```bash
git add -A && git commit -m "docs: update all documentation for package restructure"
```

---

## Post-Migration Cleanup (Optional Follow-Up Tasks)

These are not part of the core migration but worth doing afterward:

1. **Remove wildcard re-exports** — The `config/__init__.py` uses `from .settings import *` for backward compat. Once all imports are updated to use specific submodules, remove this.

2. **Audit `__init__.py` facades** — `state/__init__.py` and `host/container_manager/__init__.py` have fat re-export facades. Consider whether callers should import from specific submodules instead.

3. **Enforce layer boundaries** — Add an import linter rule (e.g., `import-linter`) to enforce that:
   - `config/` never imports from `host/` or `plugins/`
   - `state/` never imports from `host/` or `plugins/`
   - `plugins/` never imports from `host/`
   - `host/container_manager/` never imports from `host/orchestrator/`
   - Foundation modules (`types.py`, `logger.py`, `utils.py`, `event_bus.py`) never import from any package

4. **Consider splitting `host/container_manager/orchestrator.py`** — this file name collides conceptually with `host/orchestrator/`. It was originally `container_runner/_orchestrator.py` (the function that spawns containers). Consider renaming to `spawn.py` or `launcher.py`.

---

### Task 12: Rename test files to match new module names

**Context:** Tests stay flat in `tests/` but filenames should reflect the new module names so you can find tests by module name.

**Renames:**

| From | To | Reason |
|------|-----|--------|
| `tests/test_output_handler.py` | `tests/test_messaging_router.py` | `output_handler` → `messaging/router.py` |
| `tests/test_router.py` | `tests/test_messaging_formatter.py` | `chat/router` → `messaging/formatter.py` |
| `tests/test_registry.py` | `tests/test_ipc_registry.py` | Disambiguate — `registry` now exists in both plugins and ipc |
| `tests/test_db.py` | `tests/test_state.py` | `db/` → `state/` |
| `tests/test_agent_runner_main.py` | `tests/test_agent_runner.py` | Drop `_main` suffix (no longer ambiguous) |
| `tests/test_gate_lifecycle.py` | `tests/test_security_gate.py` | Clearer what it tests |
| `tests/test_policy_middleware.py` | `tests/test_security_policy.py` | Matches `security/middleware.py` |
| `tests/test_cop_gate.py` | `tests/test_security_cop_gate.py` | Prefix with subsystem |
| `tests/test_secrets_scanner.py` | `tests/test_security_secrets.py` | Prefix with subsystem |
| `tests/test_mcp_proxy.py` | `tests/test_mcp_proxy.py` | Keep — already clear |
| `tests/test_pending_questions.py` | `tests/test_messaging_pending_questions.py` | Prefix with subsystem |
| `tests/test_approval_commands.py` | `tests/test_messaging_approval.py` | Prefix with subsystem |
| `tests/test_ask_user_e2e.py` | `tests/test_messaging_ask_user_e2e.py` | Prefix with subsystem |

Leave the rest as-is — names like `test_event_bus.py`, `test_directives.py`, `test_gateway.py`, `test_utils.py` are already clear.

**Step 1: Rename files**

```bash
cd tests
git mv test_output_handler.py test_messaging_router.py
git mv test_router.py test_messaging_formatter.py
git mv test_registry.py test_ipc_registry.py
git mv test_db.py test_state.py
git mv test_agent_runner_main.py test_agent_runner.py
git mv test_gate_lifecycle.py test_security_gate.py
git mv test_policy_middleware.py test_security_policy.py
git mv test_cop_gate.py test_security_cop_gate.py
git mv test_secrets_scanner.py test_security_secrets.py
git mv test_pending_questions.py test_messaging_pending_questions.py
git mv test_approval_commands.py test_messaging_approval.py
git mv test_ask_user_e2e.py test_messaging_ask_user_e2e.py
```

**Step 2: Update any cross-references between test files**

Check `conftest.py` and any test files that import from other test files.

**Step 3: Verify and commit**

```bash
uvx pytest -x
git add -A && git commit -m "refactor: rename test files to match new module names"
```
