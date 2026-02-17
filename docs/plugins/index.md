# Plugins

Pynchy stays minimal by design. New capabilities — channels, tools, skills, agent cores — are added as **plugins**, not features in the base codebase.

Plugins come as regular Python packages, discovered automatically at startup. Install a plugin, restart Pynchy, done.

## Plugin Categories

| Category | Hook | What it provides | Runs where |
|----------|------|-----------------|------------|
| **Agent Core** | `pynchy_agent_core_info()` | LLM framework (Claude SDK, OpenAI, Ollama) | Container |
| **MCP Server** | `pynchy_mcp_server_spec()` | Tools for the agent via Model Context Protocol | Container |
| **MCP Server Handler** | `pynchy_mcp_server_handler()` | Host-side service tool handlers dispatched via IPC | Host |
| **Skill** | `pynchy_skill_paths()` | Agent instructions and capabilities (markdown) | Container |
| **Channel** | `pynchy_create_channel()` | Communication platform (Telegram, Slack, Discord) | Host |
| **Container Runtime** | `pynchy_container_runtime()` | Container runtime implementation (Docker, Apple Container) | Host |
| **Tunnel** | `pynchy_tunnel()` | Remote connectivity detection (Tailscale, Cloudflare, etc.) | Host |
| **Workspace** | `pynchy_workspace_spec()` | Managed workspace/task definitions (e.g. periodic agents) | Host |

A single plugin can implement multiple hooks. For example, a "voice" plugin might provide both an MCP server (transcription tools) and a skill (voice interaction patterns).

> **LLM Gateway:** Regardless of which Agent Core plugin is active, all LLM API calls route through a host-side [LiteLLM](https://docs.litellm.ai/docs/) gateway. This provides automatic load balancing across APIs, access to [100+ LLM providers](https://docs.litellm.ai/docs/providers), and credential isolation — containers never see real API keys. Configure it via `litellm_config.yaml` (see [Container Isolation — Environment Variable Isolation](../architecture/container-isolation.md#environment-variable-isolation)).

## How Discovery Works

```
App starts
  → get_plugin_manager() creates a pluggy PluginManager
  → Registers built-in plugins (builtin_*.py files)
  → Discovers third-party plugins via Python entry points
  → Ready: pm.hook.pynchy_agent_core_info(), etc.
```

Plugins register via `pyproject.toml` entry points in the `"pynchy"` group. Installation activates them. Uninstalling removes them. No config files needed.

## Security Model

All plugin Python code runs on the **host** during discovery. To mitigate the risk of malicious plugins, Pynchy runs an automated [**plugin scanner**](plugin-scanner.md) that audits new plugin revisions inside an isolated container before installation. Plugins marked `trusted = true` in config bypass the scan. See [Security Model](../architecture/security.md) for the full trust model.

| Category | Sandbox Level | Risk |
|----------|--------------|------|
| **Channel** | None — runs persistently in host process | Highest |
| **Container Runtime** | None — host-side process management | Highest |
| **Tunnel** | None — host-side network detection | High |
| **MCP Server Handler** | None — host-side handlers with policy enforcement | High |
| **Workspace** | Host-side config only — agent runs in container | Medium |
| **Skill** | Partial — `skill_paths()` on host, content in container | Medium |
| **MCP Server** | Mostly sandboxed — spec on host, server in container | Lower |

## Next Steps

- [**Available Plugins**](available.md) — Browse first-party plugins and community listings
- [**Cookiecutter template**](quickstart.md#1-scaffold-the-plugin) — Use [`cookiecutter-pynchy-plugin`](https://github.com/crypdick/cookiecutter-pynchy-plugin) for a ready-made scaffold
- [**Quickstart**](quickstart.md) — Build your first plugin in 5 minutes
- [**Hook Reference**](hooks.md) — All plugin hooks and return value schemas
- [**Packaging**](packaging.md) — Entry points, distribution, installation
- [**Plugin Scanner**](plugin-scanner.md) — Automated security audit for third-party plugins
