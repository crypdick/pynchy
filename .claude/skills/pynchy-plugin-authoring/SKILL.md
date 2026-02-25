---
name: Pynchy Plugin Authoring
description: Use when creating, scaffolding, or updating a pynchy plugin, including channels, MCP servers, skills, agent cores, workspace specs, and container runtime plugins. Also use when users ask how to register plugins via config.toml, add entry points, or validate plugin hook wiring.
---

# Pynchy Plugin Authoring

## When To Use

Use this skill when the user asks to:

- Create a new `pynchy` plugin
- Add a new hook to an existing plugin
- Register/enable plugins in `config.toml`
- Validate plugin discovery and runtime behavior

## Core Rules

1. Keep plugin responsibilities narrow. One plugin can implement multiple hooks, but avoid unrelated concerns in one package.
2. In the plugin repository `pyproject.toml` (not `pynchy/pyproject.toml`), define entry points under `[project.entry-points."pynchy"]`.
3. For host runtime/channel code, treat plugin code as high trust and avoid risky side effects in import-time code.
4. For plugin docs, cross-link to `pynchy/docs/plugins/*` instead of duplicating long explanations.
5. Refer to the [quickstart guide](docs/plugins/quickstart.md) for the recommended plugin directory structure.
6. **Plugin config models belong in the plugin.** Define Pydantic config models inside the plugin's own source file, not in `pynchy/src/pynchy/config_models.py`. The core `config_models.py` is for pynchy core settings only.

## File Scope Conventions

When this skill mentions config files, use this mapping:

- Host app config: `config.toml` (`[plugins.<name>]` entries enable repos)
- Host app package metadata: `pyproject.toml`
- Plugin package metadata: `<plugin-repo>/pyproject.toml` (entry points live here)
- Plugin source: `<plugin-repo>/src/<plugin_module>/...`

## Authoring Workflow

Copy this checklist and complete it in order:

```text
Plugin Authoring Checklist
- [ ] Choose plugin scope and hook categories
- [ ] Create plugin directory structure (or update existing plugin)
- [ ] Implement hook methods and runtime logic
- [ ] Add/update tests
- [ ] Configure local pynchy [plugins.<name>] entry
- [ ] Run tests and smoke checks
- [ ] Update docs if behavior is user-visible
```

## Hook Map

- `pynchy_create_channel`: Host-side channel instance
- `pynchy_service_handler`: Host-side service tool handlers (IPC dispatch)
- `pynchy_skill_paths`: Skill directories mounted into container
- `pynchy_agent_core_info`: Agent core implementation metadata
- `pynchy_container_runtime`: Host container runtime provider
- `pynchy_workspace_spec`: Managed workspace/task definitions (e.g., periodic agents)

Hook reference:
- `docs/plugins/hooks.md`

## Config-Managed Plugin Registration

For local `pynchy`, prefer config-managed plugin repos:

```toml
[plugins.example]
repo = "owner/pynchy-plugin-example"
ref = "main"
enabled = true
```

Then restart `pynchy`. Startup sync handles clone/update and host install.

If the host is remote, don't forget to ssh into it and add the plugin to the `config.toml` file.

## Validation Commands

For plugin repositories:

```bash
uv run pytest
```

For `pynchy` docs link safety after docs changes:

```bash
uv run mkdocs build --strict
```

## Security

All plugin Python code runs on the host during discovery (`__init__`, `validate()`, category methods). Installing a plugin means trusting its code. **Only install plugins from authors you trust.**

For the full risk-by-category breakdown, see [Plugin Security](docs/plugins/index.md#security-model).

## References

- Plugin overview: `docs/plugins/index.md`
- Plugin quickstart (creation guide): `docs/plugins/quickstart.md`
- Hook reference: `docs/plugins/hooks.md`
- Packaging guidance: `docs/plugins/packaging.md`
- Available plugin registry: `docs/plugins/available.md`
