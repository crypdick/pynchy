# LiteLLM Gateway — Credential Isolation & Provider Routing

Replace direct credential mounting with a host-side LiteLLM gateway. Containers never see real API keys — they get virtual keys routed through the proxy. This is a prerequisite for extracting agent backends (Claude, OpenAI) into plugins.

## Context

Today, all API credentials (Anthropic, OpenAI, GitHub, OAuth) are written to `data/env/env` and mounted into every container. A compromised container can trivially read them. Claude Code's official docs endorse the `ANTHROPIC_BASE_URL` + LLM gateway pattern ([LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)).

Discussion on 2026-02-15 concluded:

1. **LiteLLM as a Python library** embedded in the pynchy host process (not a separate service). Runs in the same asyncio event loop. Avoids managing a separate process.
2. **Trust LiteLLM at the proxy level** — assume it won't leak real keys in response headers. Combined with container network isolation (block direct access to `api.anthropic.com`), this is sufficient.
3. **Check provider backend compatibility** — verify that both the Claude and OpenAI agent cores work correctly when routed through LiteLLM's Anthropic Messages format / OpenAI format endpoints. Identify any SDK quirks (e.g., does Claude Code validate API key format before making requests? Does it rely on non-standard headers that LiteLLM strips?).

## Why This Matters

- **Security**: Real API keys never enter containers. Virtual keys are per-container, ephemeral, budget-capped.
- **Plugin extraction**: Agent core plugins stop needing credential hooks. They just make normal API calls to `BASE_URL`. Pynchy handles the rest.
- **Cost control**: LiteLLM virtual keys support per-key budgets and rate limits. Per-group spending limits become trivial.
- **Audit trail**: Every API call logged with which group/container made it.

## Required Reading

Before planning, read the full Anthropic guide on LLM gateway configuration:
https://docs.anthropic.com/en/docs/claude-code/llm-gateway

This covers gateway requirements (which headers must be forwarded), LiteLLM-specific setup (unified vs pass-through endpoints), authentication methods (static key, dynamic key helper), and provider-specific pass-through for Bedrock/Vertex. Read it end to end — the details on required header forwarding (`anthropic-beta`, `anthropic-version`) and `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` are critical.

## Planning Agenda (Next Session)

Write a full implementation plan covering:

### 1. LiteLLM integration as embedded Python library
- How to start/stop within the pynchy asyncio loop
- Configuration: model routing, virtual key storage (in-memory vs SQLite)
- Where the real keys come from (existing `config.toml [secrets]` + credential discovery)
- Error handling: what happens if the proxy crashes mid-request?

### 2. Container credential pipeline changes
- Remove real keys from `data/env/env` (`_credentials.py`)
- Mint ephemeral virtual keys per container launch
- Set `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` to proxy URL + virtual key
- Decide: per-group persistent keys vs per-container ephemeral keys

### 3. Provider backend compatibility check
- Test Claude Code (`@anthropic-ai/claude-code`) through LiteLLM's `/v1/messages` endpoint
- Test OpenAI Agents SDK through LiteLLM's `/v1/chat/completions` endpoint
- Verify: streaming, tool use, extended thinking, beta headers (`anthropic-beta`, `anthropic-version`)
- Check if `apiKeyHelper` or `ANTHROPIC_AUTH_TOKEN` works better than `ANTHROPIC_API_KEY` for virtual keys
- Document any SDK-specific workarounds needed

### 4. Network isolation
- Container network policy: block all outbound except the host proxy
- Docker: `--network=none` + mounted Unix socket, or custom bridge with iptables
- Apple Container: equivalent isolation mechanism
- Fallback: what if network isolation isn't possible on a runtime? Is the proxy still useful without it?

### 5. Impact on plugin extraction
- How this simplifies the `pynchy_agent_core_info()` hook contract
- Which new hooks are still needed (`container_env`, `container_mounts`, `prepare_session`)
- Which host-side Claude-specific code can be deleted from the container runner

## Key Files

| File | Relevance |
|------|-----------|
| `src/pynchy/container_runner/_credentials.py` | Current credential pipeline — needs rewrite |
| `src/pynchy/container_runner/_mounts.py` | Mounts `data/env/` — remove env mount |
| `container/Dockerfile` | Entrypoint sources env file — simplify |
| `src/pynchy/config.py` | `SecretsConfig` — real keys stay here, fed to LiteLLM |
| `src/pynchy/plugin/builtin_agent_claude.py` | Claude core registration — future plugin extraction |
| `src/pynchy/plugin/builtin_agent_openai.py` | OpenAI core registration — future plugin extraction |
| `docs/architecture/security.md` | Update credential isolation docs |

## Dependencies

- None. This can proceed independently of the security hardening steps (1-7), though it complements them.

## Related Items

- [Factor out Claude backend as plugin](../3-ready/) (in 3-ready, blocked on this)
- [Factor out OpenAI backend as plugin](../3-ready/) (in 3-ready, blocked on this)
- [Security Hardening](2-planning/security-hardening.md) (complementary — this solves the credential isolation gap)
