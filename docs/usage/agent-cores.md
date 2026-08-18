# Agent Cores

The agent core determines which LLM SDK or CLI powers agents. Pynchy ships with several built-in cores, and you can add more via plugins.

## Switching Cores

Set the core in `data/personalization/pynchy.toml`:

```toml
[agent]
default_core = "openai"    # or "claude", "claude-cli", "codex"
model = "openai/gpt-5.5"
```

Or via environment variable (takes priority over config):

```bash
AGENT__DEFAULT_CORE=codex
```

Restart Pynchy after changing the core.

## Execution Mode

Workspaces run in containers by default. Trusted admin workspaces can opt into direct host execution:

```toml
[profiles.local-admin]
is_admin = true
execution_mode = "host"
cwd = "/path/to/workspace"

[workspaces.local-admin]
profiles = ["local-admin"]
```

`execution_mode = "host"` runs the selected agent core as a host child process.
Static workspaces run in `cwd`; a routed conversation with a selected repository
uses its isolated child worktree instead. See [Worktree
Isolation](worktrees.md) for branch and pull-request behavior. Host execution
does not mount workspace directories or apply the container sandbox. It retains
Pynchy's built-in MCP server and the shared tool-hook roster through group-scoped
host IPC. Model routing still comes from the selected core and LiteLLM config.

Host execution requires an admin workspace and an explicit `cwd`.

## Workspace Model Overrides

Set `model` in a profile to provide a reusable default. Set it directly in a workspace when that workspace needs a different route:

```toml
[profiles.daily-triage]
model = "chatgpt/gpt-5.3-codex"

[workspaces.daily-triage]
profiles = ["daily-triage"]
model = "chatgpt/gpt-5.3-codex-spark"
```

```toml
# data/personalization/automations/daily-triage/config.toml
schema_version = 1

[job]
enabled = true
schedule = "0 8 * * *"
workspace = "daily-triage"
prompt = "Produce the daily Pynchy triage memo and send it to this channel."
```

Pynchy resolves models in this order: the workspace model, the last model specified by the expanded selected profiles, then `[agent].model`. Dynamic threads and scheduled jobs inherit their target workspace's resolved model.

For Codex workspaces using LiteLLM's ChatGPT subscription provider, use a `chatgpt/...` route such as `chatgpt/gpt-5.3-codex-spark` and make sure that `model_name` is declared in `data/personalization/litellm.yaml`. Pynchy checks every explicitly configured effective route for Codex, OpenAI, and Claude CLI at startup.

## Built-in: Claude SDK

Uses the Claude Agent SDK (Claude Code) to power agents.

- **Model selection:** fixed to `opus`; Pynchy rejects `model` settings for this core
- **Session management:** maintains conversation sessions across messages, auto-compacts when context grows too long
- **Tools:** Bash, file operations, MCP servers, and all Claude Code capabilities
- **Activation:** set `[agent] default_core = "claude"` and make sure an Anthropic API key is available

## Built-in: OpenAI Agents SDK

The default core using OpenAI's Agents SDK.

- **Activation:** selected by default; make sure an OpenAI API key is available
- **Model selection:** via the LiteLLM gateway
- **Default model:** `openai/gpt-5.5`

## Built-in: Claude Code CLI

Drives the `claude` CLI through its stream-JSON interface instead of the
Claude Agent SDK. Choose this core when you need Claude Code CLI session
behavior while retaining Pynchy's gateway routing, MCP configuration, and
shared tool-security hooks.

- **Activation:** set `[agent] default_core = "claude-cli"`
- **Model selection:** model settings route through LiteLLM; unlike the Claude
  SDK core, this core can use an explicitly configured model
- **Session management:** Pynchy resumes the CLI session between turns

## Built-in: OpenAI Codex CLI

An alternative core that drives `codex exec --json` inside the agent container.
Use this when you want Codex's CLI runtime, session behavior, and JSONL event
stream while keeping model routing and provider credentials in the Pynchy
LiteLLM gateway.

- **Activation:** configure a Codex-capable model in `data/personalization/litellm.yaml`, then set `default_core = "codex"` and `model` to that LiteLLM `model_name`
- **Auth state:** Codex reads the same gateway env vars as the OpenAI core (`OPENAI_BASE_URL` / `OPENAI_API_KEY`). Pynchy writes a per-group Codex config that points Codex at that gateway with the Responses wire API.
- **Session management:** Codex thread IDs are stored as Pynchy session IDs and resumed with `codex exec resume`
- **Tools:** Codex Bash/tool events are mapped into the same Pynchy event stream, and Pynchy writes a per-group Codex config with the standard `BEFORE_TOOL_USE` hook

The per-group Codex home still stores generated config and Codex session state,
but it does not need host `~/.codex/auth.json`. Real provider credentials stay
behind the gateway.

### Reasoning effort

For the Codex CLI core, set `model_reasoning_effort` under `[agent]` to select
the default effort for Pynchy-managed Codex sessions. Set it on a workspace or
semantic workspace when that workspace needs a different effort. Dynamic
threads and scheduled jobs inherit their target workspace's resolved effort.
Pynchy writes the resolved setting to each session's generated Codex
configuration, so host-level Codex settings don't affect those sessions.

```toml
[agent]
default_core = "codex"
model = "gpt-5.6-terra"
model_reasoning_effort = "ultra"
```

```toml
[workspaces.security-fixer]
profiles = ["code"]
model = "gpt-5.6-terra"
model_reasoning_effort = "medium"
```

GPT-5.6 Terra supports `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`
in Codex. Other models and accounts can expose a different subset.

## Tool Security

All built-in cores share the `BEFORE_TOOL_USE` hook pipeline. Built-in security hooks run first; plugin-provided hooks run after.

**Agent tool security gate.** File and shell operations pass through one semantic gate before execution. File-capable tools establish workspace taint, deterministic hazards get blocked locally, and the Cop approves, denies, or escalates tainted network-capable commands. CLI hook payload errors, built-in gate exceptions, and unavailable Bash policy responses deny the tool call. See [Agent Tool Gating](security.md#agent-tool-gating).

**WebFetch removal.** The `WebFetch` tool is gone from both cores. Web access goes through the Playwright browser MCP server, which is gated by the standard tool trust policy.

**Extensibility.** Plugins register lifecycle modules through `pynchy_agent_hook_specs`. A module can export `before_tool_use(tool_name, tool_input)` and return a `HookDecision`. See [Agent core and lifecycle hooks](../plugins/hooks/agent-cores.md#pynchy_agent_hook_specs).

## LLM Gateway

Claude SDK and OpenAI Agents SDK calls route through a host-side gateway. You get:

- **Credential isolation** — containers never see real API keys (see [Security Model](../architecture/security.md#6-credential-handling))
- **Provider flexibility** — [100+ LLM providers](https://docs.litellm.ai/docs/providers) via LiteLLM
- **Load balancing** — across multiple API keys or providers

The gateway is configured in `data/personalization/litellm.yaml` and runs as a Docker container managed by Pynchy. See the [Installation Guide](../installation/index.md).

Pynchy's local LLM-request redaction runs only when using the built-in gateway.
LiteLLM bypasses the owned Python request boundary and reports
`redaction = "not_enforced"` in `/status`. See [LLM request
redaction](../architecture/security.md#llm-request-redaction) for the exact
scope and restoration restrictions.

The Codex CLI core also uses the gateway. Pynchy generates a Codex custom model
provider for each workspace with:

- `model_provider = "pynchy_litellm"`
- `wire_api = "responses"`
- `env_key = "OPENAI_API_KEY"`

Use model names that exist in `data/personalization/litellm.yaml`; Codex passes its requested
model to the gateway. For LiteLLM's ChatGPT Subscription provider, that means a
route such as:

```toml
[agent]
default_core = "codex"
model = "chatgpt/gpt-5.3-codex"
```

---

**Want to customize this?** Write your own agent core plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
