# Agent Cores

The agent core determines which LLM SDK or CLI powers agents inside the container. Pynchy ships with several built-in cores, and you can add more (Ollama, local models, etc.) via plugins.

## Switching Cores

Set the core in `config.toml`:

```toml
[agent]
core = "openai"    # or "claude", "claude-cli", "codex"
model = "gpt-5.5"
```

Or via environment variable (takes priority over config):

```bash
PYNCHY_AGENT_CORE=codex
```

Restart Pynchy after changing the core.

## Profile Model Overrides

Use `model` and `fallback_model` inside a profile when a workspace or scheduled job should use a cheaper route than the global interactive agent:

```toml
[profiles.daily-triage]
context_mode = "isolated"
model = "chatgpt/gpt-5.3-codex-spark"

[workspaces.daily-triage]
profile = "daily-triage"
chat = "connection.slack.synapse.chat.pynchy"

[jobs.daily-triage]
enabled = true
schedule = "0 8 * * *"
workspace = "daily-triage"
prompt = "Produce the daily Pynchy triage memo and send it to this channel."
```

The override is resolved through the same profile cascade as `repo_access` and `git_policy`: `[profiles.<name>]` wins over `[universal]`, then Pynchy falls back to `[agent].model`.

When a profile sets `model`, it owns its fallback policy too. It will not inherit `[agent].fallback_model` unless `fallback_model` is also set in that profile cascade.

For Codex workspaces using LiteLLM's ChatGPT subscription provider, use a `chatgpt/...` route such as `chatgpt/gpt-5.3-codex-spark` and make sure that `model_name` is declared in `litellm_config.yaml`.

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
- **Default model:** `gpt-5.5`

## Built-in: OpenAI Codex CLI

An alternative core that drives `codex exec --json` inside the agent container.
Use this when you want Codex's CLI runtime, session behavior, and JSONL event
stream while keeping model routing and provider credentials in the Pynchy
LiteLLM gateway.

- **Activation:** configure a Codex-capable model in `litellm_config.yaml`, then set `core = "codex"` and `model` to that LiteLLM `model_name`
- **Auth state:** Codex reads the same gateway env vars as the OpenAI core (`OPENAI_BASE_URL` / `OPENAI_API_KEY`). Pynchy writes a per-group Codex config that points Codex at that gateway with the Responses wire API.
- **Session management:** Codex thread IDs are stored as Pynchy session IDs and resumed with `codex exec resume`
- **Tools:** Codex Bash/tool events are mapped into the same Pynchy event stream, and Pynchy writes a per-group Codex config with the standard `BEFORE_TOOL_USE` hook

The per-group Codex home still stores generated config and Codex session state,
but it does not need host `~/.codex/auth.json`. Real provider credentials stay
behind the gateway.

## Tool Security

All built-in cores share the `BEFORE_TOOL_USE` hook pipeline. Built-in security hooks run first; plugin-provided hooks run after.

**Bash security gate.** Every Bash tool call is intercepted before execution. Safe commands (file operations, text processing) run immediately; network-capable commands are checked against the session's taint state and may require Cop review or human approval. The agent doesn't see this unless a command is blocked. See [Bash Command Gating](security.md#bash-command-gating).

**WebFetch removal.** The `WebFetch` tool is gone from both cores. Web access goes through the Playwright browser MCP server, which is gated by the standard tool trust policy.

**Extensibility.** Plugins can register their own `BEFORE_TOOL_USE` hooks — a module exporting `before_tool_use(tool_name, tool_input)` that returns a `HookDecision`. See the [Plugin Authoring Guide](../plugins/index.md).

## LLM Gateway

Claude SDK and OpenAI Agents SDK calls route through a host-side gateway. You get:

- **Credential isolation** — containers never see real API keys (see [Security Model](../architecture/security.md#6-credential-handling))
- **Provider flexibility** — [100+ LLM providers](https://docs.litellm.ai/docs/providers) via LiteLLM
- **Load balancing** — across multiple API keys or providers

The gateway is configured in `litellm_config.yaml` and runs as a Docker container managed by Pynchy. See the [Installation Guide](../install.md).

The Codex CLI core also uses the gateway. Pynchy generates a Codex custom model
provider for each workspace with:

- `model_provider = "pynchy_litellm"`
- `wire_api = "responses"`
- `env_key = "OPENAI_API_KEY"`

Use model names that exist in `litellm_config.yaml`; Codex passes its requested
model to the gateway. For LiteLLM's ChatGPT Subscription provider, that means a
route such as:

```toml
[agent]
core = "codex"
model = "chatgpt/gpt-5.3-codex"
```

---

**Want to customize this?** Write your own agent core plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
