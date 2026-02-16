---
name: Pynchy Plugin Authoring
description: Use when creating, scaffolding, or updating a pynchy plugin, including channels, MCP servers, skills, agent cores, and container runtime plugins. Also use when users ask how to register plugins via config.toml, add entry points, or validate plugin hook wiring.
---

# Pynchy Plugin Authoring

## When To Use

Use this skill when the user asks to:

- Create a new `pynchy` plugin
- Add a new hook to an existing plugin
- Scaffold plugin files from `cookiecutter-pynchy-plugin`
- Register/enable plugins in `config.toml`
- Validate plugin discovery and runtime behavior

## Core Rules

1. Prefer scaffolding with the local cookiecutter template:
   - `<path-to-cookiecutter-pynchy-plugin>` (often `../cookiecutter-pynchy-plugin` if repos are siblings)
2. Keep plugin responsibilities narrow. One plugin can implement multiple hooks, but avoid unrelated concerns in one package.
3. In the plugin repository `pyproject.toml` (not `pynchy/pyproject.toml`), define entry points under `[project.entry-points."pynchy"]`.
4. For host runtime/channel code, treat plugin code as high trust and avoid risky side effects in import-time code.
5. For plugin docs, cross-link to `pynchy/docs/plugins/*` instead of duplicating long explanations.

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
- [ ] Scaffold with cookiecutter (or update existing plugin)
- [ ] Implement hook methods and runtime logic
- [ ] Add/update tests
- [ ] Configure local pynchy [plugins.<name>] entry
- [ ] Run tests and smoke checks
- [ ] Update docs if behavior is user-visible
```

## Recommended Scaffold Command

From any working directory:

```bash
uvx cookiecutter --no-input \
  https://github.com/crypdick/cookiecutter-pynchy-plugin.git \
  plugin_slug="<slug>" \
  plugin_repo_name="pynchy-plugin-<slug>" \
  python_module="pynchy_plugin_<slug_with_underscores>" \
  plugin_class_name="<PascalCase>Plugin" \
  entry_point_name="<slug>" \
  plugin_description="<short description>" \
  include_mcp_server="no" \
  include_skill="no" \
  include_agent_core="no" \
  include_channel="no" \
  include_container_runtime="no" \
  include_tests="yes"
```

You can also replace the URL with a local template path when iterating on template changes.

Enable the needed `include_*` flags for the plugin type.

## Hook Map

- `pynchy_create_channel`: Host-side channel instance
- `pynchy_mcp_server_spec`: Container MCP server spec
- `pynchy_skill_paths`: Skill directories mounted into container
- `pynchy_agent_core_info`: Agent core implementation metadata
- `pynchy_container_runtime`: Host container runtime provider

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

## Validation Commands

For plugin repositories:

```bash
uv run pytest
```

For cookiecutter template repository:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

For `pynchy` docs link safety after docs changes:

```bash
uv run mkdocs build --strict
```

## References

- Plugin overview: `docs/plugins/index.md`
- Plugin quickstart (creation guide): `docs/plugins/quickstart.md`
- Hook reference: `docs/plugins/hooks.md`
- Packaging guidance: `docs/plugins/packaging.md`
- Available plugin registry: `docs/plugins/available.md`
