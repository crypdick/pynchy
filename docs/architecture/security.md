# Pynchy Security Model

## Trust Model

| Entity | Trust Level | Rationale |
|--------|-------------|-----------|
| God group | Trusted | Private self-chat, admin control |
| Non-god groups | Untrusted | Other users may be malicious |
| Container agents | Sandboxed | Isolated execution environment |
| WhatsApp messages | User input | Potential prompt injection |

## Security Boundaries

### 1. Container Isolation (Primary Boundary)

Agents execute in Apple Container (macOS) or Docker (Linux), providing:
- **Process isolation** - Container processes cannot affect the host
- **Filesystem isolation** - Only explicitly mounted directories are visible
- **Non-root execution** - Runs as unprivileged `agent` user
- **Ephemeral containers** - Fresh environment per invocation (`--rm`)

This is the primary security boundary. Rather than relying on application-level permission checks, the attack surface is limited by what's mounted.

### 2. Mount Security

**External Allowlist** - Mount permissions stored at `~/.config/pynchy/mount-allowlist.toml`, which is:
- Outside project root
- Never mounted into containers
- Cannot be modified by agents

**Default Blocked Patterns:**
```
.ssh, .gnupg, .gpg, .aws, .azure, .gcloud, .kube, .docker,
credentials, .env, .netrc, .npmrc, .pypirc, id_rsa, id_ed25519,
private_key, .secret
```

**Protections:**
- Symlink resolution before validation (prevents traversal attacks)
- Container path validation (rejects `..` and absolute paths)
- `non_god_read_only` option forces read-only for non-god groups

### 3. Session Isolation

Each group has isolated Claude sessions at `data/sessions/{group}/.claude/`:
- Groups cannot see other groups' conversation history
- Session data includes full message history and file contents read
- Prevents cross-group information disclosure

### 4. IPC Authorization

Messages and task operations are verified against group identity:

| Operation | God Group | Non-God Group |
|-----------|------------|----------------|
| Send message to own chat | ✓ | ✓ |
| Send message to other chats | ✓ | ✗ |
| Schedule task for self | ✓ | ✓ |
| Schedule task for others | ✓ | ✗ |
| View all tasks | ✓ | Own only |
| Manage other groups | ✓ | ✗ |

### 5. Credential Handling

#### LLM Gateway (default)

When `gateway.enabled = true` (the default), an LLM API gateway runs on the host process and proxies container API calls to real providers. Containers **never see real LLM API keys**.

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
- Real API keys exist only in the host process memory
- Ephemeral keys are per-session (regenerated on restart), not real credentials
- A compromised container cannot use the ephemeral key outside the gateway
- Docker containers reach the host via `host.docker.internal` (with `--add-host` on Linux)

**Non-LLM credentials** are written directly to per-group env files (`data/env/{group}/env`):

| Credential | God | Non-God | Rationale |
|-----------|-----|---------|-----------|
| `GH_TOKEN` | Yes | **No** | Non-god containers have git push/pull blocked by the guard script and routed through host IPC. They never need direct GitHub access. |
| `GIT_AUTHOR_NAME` | Yes | Yes | Needed for git commits in worktrees |
| `GIT_COMMITTER_NAME` | Yes | Yes | |
| `GIT_AUTHOR_EMAIL` | Yes | Yes | |
| `GIT_COMMITTER_EMAIL` | Yes | Yes | |

Each group gets its own env directory so concurrent containers don't share secrets. A compromised non-god container cannot access GitHub APIs or push to repositories directly.

**NOT Mounted:**
- WhatsApp session (`store/auth/`) — host only
- Mount allowlist — external, never mounted
- Any credentials matching blocked patterns

### 6. Prompt Injection

WhatsApp messages could contain malicious instructions attempting to manipulate Claude's behavior.

**Mitigations:**
- Container isolation limits blast radius of successful attacks
- Only registered groups are processed (explicit allowlist)
- Trigger word required (reduces accidental processing)
- Agents can only access their group's mounted directories
- Additional directory mounts must be explicitly configured per group
- Claude's built-in safety training helps resist manipulation

**Recommendations:**
- Only register trusted groups
- Review additional directory mounts carefully before adding
- Review scheduled tasks periodically for unexpected behavior
- Monitor logs for unusual activity
- Use `groups/global/` for shared readonly resources only

## Privilege Comparison

| Capability | God Group | Non-God Group |
|------------|------------|----------------|
| Project root access | `/workspace/project` (rw) | Via `project_access` (worktree, rw) |
| Group folder | `/workspace/group` (rw) | `/workspace/group` (rw) |
| Global memory | Implicit via project | `/workspace/global` (ro) |
| Additional mounts | Configurable | Read-only unless allowed |
| Network access | Unrestricted | Unrestricted |
| MCP tools | All | All |

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
