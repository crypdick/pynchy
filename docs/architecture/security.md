# Pynchy Security Model

This page covers Pynchy's security boundaries, trust model, and credential handling. Read this to understand what agents can and cannot access, and how to evaluate the risk of adding mounts, plugins, or new groups.

## Trust Model

| Entity | Trust Level | Rationale |
|--------|-------------|-----------|
| Admin group | Trusted | Private self-chat, admin control |
| Non-admin groups | Untrusted | Other users may be malicious |
| Container agents | Sandboxed | Isolated execution environment |
| WhatsApp messages | User input | Potential prompt injection |

## Security Boundaries

### 1. Container Isolation (Primary Boundary)

Agents execute in Apple Container (macOS) or Docker (Linux), providing:
- **Process isolation** — container processes cannot affect the host
- **Filesystem isolation** — only explicitly mounted directories appear inside the container
- **Non-root execution** — runs as unprivileged `agent` user
- **Ephemeral containers** — fresh environment per invocation (`--rm`)

The container boundary limits the attack surface to what gets mounted, rather than relying on application-level permission checks.

### 2. Mount Security

**External Allowlist** — Mount permissions live at `~/.config/pynchy/mount-allowlist.toml`:
- Stored outside the project root
- Never mounted into containers
- Agents cannot modify it

**Default Blocked Patterns:**
```
.ssh, .gnupg, .gpg, .aws, .azure, .gcloud, .kube, .docker,
credentials, .env, .netrc, .npmrc, .pypirc, id_rsa, id_ed25519,
private_key, .secret
```

**Protections:**
- Symlink resolution before validation (prevents traversal attacks)
- Container path validation (rejects `..` and absolute paths)
- `non_admin_read_only` option enforces read-only for non-admin groups

### 3. Session Isolation

Each group has isolated Claude sessions at `data/sessions/{group}/.claude/`:
- Groups cannot see other groups' conversation history
- Session data includes full message history and file contents read
- This prevents cross-group information disclosure

### 4. IPC Authorization

The host verifies messages and task operations against group identity:

| Operation | Admin Group | Non-Admin Group |
|-----------|------------|----------------|
| Send message to own chat | ✓ | ✓ |
| Send message to other chats | ✓ | ✗ |
| Schedule task for self | ✓ | ✓ |
| Schedule task for others | ✓ | ✗ |
| View all tasks | ✓ | Own only |
| Manage other groups | ✓ | ✗ |

### 5. MCP Service Tool Policy

Host-side MCP service tools (calendar, etc.) are gated by a policy middleware. Tools are assigned to risk tiers:

- **always-approve** — low-risk read operations, executed without checks
- **rules-engine** — deterministic rules (auto-approved for now; future: contextual rules like "only your own calendar")
- **human-approval** — high-risk operations, denied until a human approves via chat

Admin workspaces bypass all policy gates — all service tools are auto-approved. Non-admin workspaces fall back to strict defaults (human-approval for all tools) unless a security profile is configured.

### 6. Credential Handling

#### LLM Gateway (default)

When `gateway.enabled = true` (the default), an LLM API gateway runs on the host and proxies container API calls to real providers. Containers **never see real LLM API keys**.

**How it works:**

```
Container ──[gateway key]──► Host Gateway ──[real API key]──► Provider
```

1. The gateway discovers real credentials (Anthropic API key / OAuth token, OpenAI API key) from `config.toml [secrets]` and auto-discovery (Claude Code keychain, etc.).
2. On startup, a random per-session ephemeral key (`gw-…`) is generated.
3. Containers receive environment variables pointing to the gateway:

```
ANTHROPIC_BASE_URL=http://host.docker.internal:4010
ANTHROPIC_AUTH_TOKEN=gw-<random>
OPENAI_BASE_URL=http://host.docker.internal:4010
OPENAI_API_KEY=gw-<random>
```

4. The gateway validates the ephemeral key, then forwards requests to the real provider with real credentials injected. Responses stream back transparently.
5. Required headers (`anthropic-beta`, `anthropic-version`) are forwarded to the provider.

**Security properties:**
- Real API keys exist only in host process memory
- Ephemeral keys regenerate on each restart and carry no value outside the gateway
- A compromised container cannot use the ephemeral key to reach providers directly
- Docker containers reach the host via `host.docker.internal` (with `--add-host` on Linux)

**Non-LLM credentials** get written directly to per-group env files (`data/env/{group}/env`):

| Credential | Admin | Non-Admin | Rationale |
|-----------|-----|---------|-----------|
| `GH_TOKEN` | Yes (broad) | **Repo-scoped** (if configured) | Admin gets the host's broad token. Non-admin containers with `repo_access` get a fine-grained PAT scoped to their designated repo (configured via `repos."owner/repo".token`). Non-admin containers without `repo_access` get no token. |
| `GIT_AUTHOR_NAME` | Yes | Yes | Needed for git commits in worktrees |
| `GIT_COMMITTER_NAME` | Yes | Yes | |
| `GIT_AUTHOR_EMAIL` | Yes | Yes | |
| `GIT_COMMITTER_EMAIL` | Yes | Yes | |

Each group gets its own env directory, so concurrent containers don't share secrets. A compromised non-admin container's token is scoped to a single repo and cannot access other repositories.

**Token resolution order** for host-side git operations (fetch, push, ls-remote):

1. `repos."owner/repo".token` — explicit per-repo fine-grained PAT (highest priority)
2. `secrets.gh_token` — host's broad token (fallback for repos without a scoped token)
3. `gh auth token` — auto-discovered from `gh` CLI (lowest priority)

**NOT Mounted:**
- WhatsApp session (`store/auth/`) — host only
- Mount allowlist — external, never mounted
- Any credentials matching blocked patterns

### 7. Prompt Injection

Channel messages can contain malicious instructions that attempt to manipulate Claude's behavior.

**Mitigations:**
- Container isolation limits the blast radius of successful attacks
- Only registered groups get processed (explicit allowlist)
- Trigger word requirement reduces accidental processing
- Agents can only access their group's mounted directories
- Additional directory mounts require explicit per-group configuration
- Claude's built-in safety training helps resist manipulation

**Recommendations:**
- Only register trusted groups
- Review additional directory mounts carefully before adding
- Review scheduled tasks periodically for unexpected behavior
- Monitor logs for unusual activity

## Privilege Comparison

| Capability | Admin Group | Non-Admin Group |
|------------|------------|----------------|
| Project root access | `/workspace/project` (rw) | Via `repo_access` (worktree, rw) |
| Group folder | `/workspace/group` (rw) | `/workspace/group` (rw) |
| System prompt directives | Scoped via config | Scoped via config |
| `config.toml` | Mounted read-write | Not mounted |
| Additional mounts | Configurable | Read-only unless allowed |
| Network access | Unrestricted | Unrestricted |
| MCP service tools | Auto-approved | Policy-gated (see below) |

## Security Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        UNTRUSTED ZONE                             │
│  WhatsApp Messages (potentially malicious)                        │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Trigger check, input escaping
┌──────────────────────────────────────────────────────────────────┐
│                     HOST PROCESS (TRUSTED)                        │
│  • Message routing                                                │
│  • IPC authorization                                              │
│  • Mount validation (external allowlist)                          │
│  • Container lifecycle                                            │
│  • LLM Gateway (credential-isolating reverse proxy)               │
│    ┌──────────────────────────────────────────────────┐           │
│    │ Container ──[gw key]──► Gateway ──[real key]──► Provider │   │
│    └──────────────────────────────────────────────────┘           │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Explicit mounts only, gateway URL in env
┌──────────────────────────────────────────────────────────────────┐
│                CONTAINER (ISOLATED/SANDBOXED)                     │
│  • Agent execution                                                │
│  • Bash commands (sandboxed)                                      │
│  • File operations (limited to mounts)                            │
│  • LLM API access via gateway only (no real keys)                 │
│  • Cannot modify security config                                  │
└──────────────────────────────────────────────────────────────────┘
```
