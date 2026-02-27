# Agent Cores

This page explains how to choose which LLM framework powers your agents. The agent core determines which SDK and model provider the agent uses inside its container.

Agent cores are pluggable. Pynchy ships with two built-in cores, and you can add more (Ollama, local models, etc.) via plugins.

## Switching Cores

Set the core in `config.toml`:

```toml
[agent]
core = "claude"    # or "openai"
```

Or via environment variable (takes priority over config):

```bash
PYNCHY_AGENT_CORE=openai
```

Restart Pynchy after changing the core.

## Built-in: Claude SDK

The default core. Uses the Claude Agent SDK (Claude Code) to power agents.

- **Model selection:** Configured via the LiteLLM gateway (see below)
- **Session management:** Maintains conversation sessions across messages, with auto-compaction when context grows too long
- **Tools:** Full access to Bash, file operations, MCP servers, and all Claude Code capabilities

## Built-in: OpenAI Agents SDK

An alternative core using OpenAI's Agents SDK.

- **Activation:** Set `core = "openai"` in config and ensure an OpenAI API key is available
- **Model selection:** Configured via the LiteLLM gateway

## Tool Security

Both cores share the same `BEFORE_TOOL_USE` hook pipeline. Built-in hooks run first (security), followed by any plugin-provided hooks.

**Bash security gate.** Every Bash tool call is intercepted before execution. Safe commands (file operations, text processing) run immediately; network-capable commands are evaluated against the session's taint state and may require Cop review or human approval. This is transparent to the agent unless a command is blocked. See [Bash Command Gating](security.md#bash-command-gating) for details.

**WebFetch removal.** The `WebFetch` tool has been removed from both cores. Web access is now provided exclusively through the Playwright browser MCP server, which goes through the standard service trust policy as a declared MCP with its own trust properties.

**Extensibility.** The `BEFORE_TOOL_USE` hook is extensible via plugins. A plugin can register a hook module that exports a `before_tool_use(tool_name, tool_input)` function returning a `HookDecision`. See the [Plugin Authoring Guide](../plugins/index.md) for details.

## LLM Gateway

Regardless of which core is active, all LLM API calls route through a host-side gateway. This provides:

- **Credential isolation** — containers never see real API keys (see [Security Model](../architecture/security.md#6-credential-handling))
- **Provider flexibility** — access [100+ LLM providers](https://docs.litellm.ai/docs/providers) via LiteLLM
- **Load balancing** — distribute requests across multiple API keys or providers

The gateway is configured in `litellm_config.yaml` and runs as a Docker container managed by Pynchy. See the [Installation Guide](../install.md) for setup details.

**Key point:** The agent core (Claude SDK vs OpenAI SDK) and the gateway are independent systems. Switching cores doesn't require changing your gateway config, and the gateway can route to any provider regardless of which SDK is in use.

---

**Want to customize this?** Write your own agent core plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
