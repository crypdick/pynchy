# Agent Cores

The agent core determines which LLM SDK or CLI powers agents inside the container. Pynchy ships with several built-in cores, and you can add more (Ollama, local models, etc.) via plugins.

## Switching Cores

Set the core in `config.toml`:

```toml
[agent]
core = "openai"    # or "claude", "claude-cli", "codex"
```

Or via environment variable (takes priority over config):

```bash
PYNCHY_AGENT_CORE=codex
```

Restart Pynchy after changing the core.

## Built-in: Claude SDK

Uses the Claude Agent SDK (Claude Code) to power agents.

- **Model selection:** via the LiteLLM gateway (see below)
- **Session management:** maintains conversation sessions across messages, auto-compacts when context grows too long
- **Tools:** Bash, file operations, MCP servers, and all Claude Code capabilities
- **Activation:** set `core = "claude"` in config and make sure an Anthropic API key is available

## Built-in: OpenAI Agents SDK

The default core using OpenAI's Agents SDK.

- **Activation:** selected by default; make sure an OpenAI API key is available
- **Model selection:** via the LiteLLM gateway

## Built-in: OpenAI Codex CLI

An alternative core that drives `codex exec --json` inside the agent container.
Use this when you want Pynchy turns billed against your Codex/ChatGPT
subscription instead of OpenAI Platform API usage.

- **Activation:** run `codex login` on the host, then set `core = "codex"`
- **Auth state:** Pynchy creates `data/sessions/{group}/.codex/` and copies the host `~/.codex/auth.json` there on first use if it exists
- **Session management:** Codex thread IDs are stored as Pynchy session IDs and resumed with `codex exec resume`
- **Tools:** Codex Bash/tool events are mapped into the same Pynchy event stream, and Pynchy writes a per-group Codex config with the standard `BEFORE_TOOL_USE` hook

The Codex CLI must be able to read Codex auth in the container. That means this
core cannot provide the same API-key isolation as the LiteLLM gateway. Treat
`data/sessions/*/.codex/auth.json` as a secret, and only enable the Codex core
for trusted sandboxes.

## Tool Security

All built-in cores share the `BEFORE_TOOL_USE` hook pipeline. Built-in security hooks run first; plugin-provided hooks run after.

**Bash security gate.** Every Bash tool call is intercepted before execution. Safe commands (file operations, text processing) run immediately; network-capable commands are checked against the session's taint state and may require Cop review or human approval. The agent doesn't see this unless a command is blocked. See [Bash Command Gating](security.md#bash-command-gating).

**WebFetch removal.** The `WebFetch` tool is gone from both cores. Web access goes through the Playwright browser MCP server, which is gated by the standard service trust policy.

**Extensibility.** Plugins can register their own `BEFORE_TOOL_USE` hooks — a module exporting `before_tool_use(tool_name, tool_input)` that returns a `HookDecision`. See the [Plugin Authoring Guide](../plugins/index.md).

## LLM Gateway

Claude SDK and OpenAI Agents SDK calls route through a host-side gateway. You get:

- **Credential isolation** — containers never see real API keys (see [Security Model](../architecture/security.md#6-credential-handling))
- **Provider flexibility** — [100+ LLM providers](https://docs.litellm.ai/docs/providers) via LiteLLM
- **Load balancing** — across multiple API keys or providers

The gateway is configured in `litellm_config.yaml` and runs as a Docker container managed by Pynchy. See the [Installation Guide](../install.md).

The Codex CLI core is the exception: it calls Codex directly through the `codex`
binary so it can use ChatGPT/Codex subscription auth. Switching between
`claude`, `claude-cli`, and `openai` uses the gateway; switching to `codex`
uses the per-group Codex CLI home instead.

---

**Want to customize this?** Write your own agent core plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
