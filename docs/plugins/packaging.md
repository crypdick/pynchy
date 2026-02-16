# Packaging & Distribution

This page covers how to package, distribute, and install pynchy plugins. Follow these conventions to make your plugin discoverable and installable by other users.

## Plugin Package Structure

```
pynchy-plugin-{name}/
├── pyproject.toml
└── src/
    └── pynchy_plugin_{name}/
        ├── __init__.py       # Plugin class with @hookimpl methods
        ├── server.py         # MCP server (if applicable)
        └── skills/           # Skill directories (if applicable)
            └── {skill-name}/
                └── SKILL.md
```

## Entry Point Registration

Pynchy discovers plugins via Python entry points. In your `pyproject.toml`:

```toml
[project.entry-points."pynchy"]
my-plugin = "pynchy_plugin_name:MyPlugin"
```

The group **must** be `"pynchy"`. The key (left side) provides a human-readable name. The value points to your plugin class.

## Naming Conventions

| What | Convention | Example |
|------|-----------|---------|
| Package name | `pynchy-plugin-{name}` | `pynchy-plugin-voice` |
| Module name | `pynchy_plugin_{name}` | `pynchy_plugin_voice` |
| Entry point key | Short descriptive name | `voice` |

## Installation Methods

### From PyPI

```bash
uv pip install pynchy-plugin-voice
```

### From Git

```bash
uv pip install git+https://github.com/user/pynchy-plugin-voice.git
```

For private repositories:

```bash
uv pip install git+https://${GITHUB_TOKEN}@github.com/user/pynchy-plugin-voice.git
```

### Local Development

```bash
uv pip install -e /path/to/pynchy-plugin-voice
```

Editable installs reflect code changes immediately — no reinstall needed.

## Verifying Installation

```bash
# Check the plugin is installed
uv pip list | grep pynchy-plugin

# Start pynchy and check logs for discovery
# Look for: "Discovered third-party plugins  count=N"
```

## Uninstalling

```bash
uv pip uninstall pynchy-plugin-voice
```

Restart pynchy — the plugin's hooks are no longer called.

## Container Dependencies

Plugins that provide container-side code (MCP servers, agent cores) need their dependencies available inside the container.

**Packages already in the container image:** `mcp`, `croniter`, Python standard library, Claude Agent SDK.

**If your plugin needs additional packages,** document this in your README. Users add them to their container Dockerfile:

```dockerfile
RUN pip install openai-whisper
```

## Version Management

Your lockfile (`uv.lock`) tracks plugin versions, just like any Python dependency:

```bash
# Pin a specific version
uv pip install pynchy-plugin-voice==1.2.3

# Upgrade
uv pip install --upgrade pynchy-plugin-voice
```

## Publishing to PyPI

Standard Python packaging workflow:

```bash
uv build
uv publish
```

See [PyPI publishing docs](https://packaging.python.org/en/latest/tutorials/packaging-projects/) for details on API tokens and metadata.
