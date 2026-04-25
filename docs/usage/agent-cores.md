# Agent Cores

The agent core determines which LLM SDK and model provider your agents use inside the container. Pynchy ships with two built-in cores, and you can add more (Ollama, local models, etc.) via plugins.

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

- **Model selection:** via the LiteLLM gateway (see below)
- **Session management:** maintains conversation sessions across messages, auto-compacts when context grows too long
- **Tools:** Bash, file operations, MCP servers, and all Claude Code capabilities

## Built-in: OpenAI Agents SDK

An alternative core using OpenAI's Agents SDK.

- **Activation:** set `core = "openai"` in config and make sure an OpenAI API key is available
- **Model selection:** via the LiteLLM gateway

## Tool Security

Both cores share the `BEFORE_TOOL_USE` hook pipeline. Built-in security hooks run first; plugin-provided hooks run after.

**Bash security gate.** Every Bash tool call is intercepted before execution. Safe commands (file operations, text processing) run immediately; network-capable commands are checked against the session's taint state and may require Cop review or human approval. The agent doesn't see this unless a command is blocked. See [Bash Command Gating](security.md#bash-command-gating).

**WebFetch removal.** The `WebFetch` tool is gone from both cores. Web access goes through the Playwright browser MCP server, which is gated by the standard service trust policy.

**Extensibility.** Plugins can register their own `BEFORE_TOOL_USE` hooks — a module exporting `before_tool_use(tool_name, tool_input)` that returns a `HookDecision`. See the [Plugin Authoring Guide](../plugins/index.md).

## LLM Gateway

All LLM API calls — from either core — route through a host-side gateway. You get:

- **Credential isolation** — containers never see real API keys (see [Security Model](../architecture/security.md#6-credential-handling))
- **Provider flexibility** — [100+ LLM providers](https://docs.litellm.ai/docs/providers) via LiteLLM
- **Load balancing** — across multiple API keys or providers

The gateway is configured in `litellm_config.yaml` and runs as a Docker container managed by Pynchy. See the [Installation Guide](../install.md).

The core and the gateway are independent. Switching cores doesn't require changing your gateway config, and the gateway routes to any provider regardless of which SDK runs.

---

**Want to customize this?** Write your own agent core plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
