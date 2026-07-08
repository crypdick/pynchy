# OneCLI Agent Vault Integration Design

## Context

Pynchy already keeps LLM provider keys out of agent containers by routing model traffic through its host gateway. Other credential paths still expose raw values to containers or sidecar processes: `GH_TOKEN` in per-group env files, MCP `env_forward` values passed as Docker/script env, and framework auth files copied into per-group session homes.

OneCLI's documented solution is not a secret-reading MCP. Its integration contract is:

- fetch container configuration from OneCLI (`/v1/container-config`) for a specific agent identifier;
- apply the returned proxy environment, CA certificate, and credential stubs to the container;
- use `onecli-managed` placeholders when a tool or MCP server needs a local credential-shaped file or env var to start;
- let OneCLI inject real credentials at request time through its gateway and enforce its agent, secret, app-connection, rule, and approval policies.

The Pynchy integration follows that contract instead of building a parallel credential broker.

## Approaches Considered

### Recommended: OneCLI-native container setup

Pynchy maps each workspace to a OneCLI agent identifier, fetches OneCLI's container config before spawn, writes any returned CA/stub files into Pynchy-managed per-group state, and applies the returned proxy env vars to the agent container. MCP sidecars use the same OneCLI config and stubs when their server definitions opt in.

This keeps raw secrets outside containers, matches OneCLI's documented agent-orchestrator model, and leaves policy enforcement in OneCLI.

### Rejected: Procedurally generated secret MCP

Pynchy could generate an MCP server per container that exposes tools for reading or brokering specific secrets. That superficially gives fine-grained control, but it recreates a secret-distribution layer. If an agent can call a tool that returns a credential, the secret can be leaked through logs, prompts, files, or outbound traffic.

This conflicts with OneCLI's core invariant: agents receive placeholders and gateway configuration, not raw secrets.

### Deferred: Full replacement of Pynchy's LLM gateway

Pynchy could route all LLM provider credentials through OneCLI and retire its builtin/LiteLLM gateway. That is a larger product decision because Pynchy's existing gateway also handles LiteLLM routing, MCP synchronization, model configuration, and existing environment contracts. The first integration does not remove that surface.

## Proposed Design

### Configuration

Add a core OneCLI configuration section:

```toml
[onecli]
enabled = true
url = "http://localhost:10254"
api_key_env = "ONECLI_API_KEY" # pragma: allowlist secret - env var name, not a value
project_id_env = "ONECLI_PROJECT_ID"
fail_closed = true
agent_identifier_prefix = "pynchy"
```

Secrets stay out of `config.toml`; Pynchy reads the API key from the named host environment variable. If `enabled = false`, Pynchy preserves today's credential behavior. If `enabled = true` and `fail_closed = true`, container startup fails when OneCLI cannot provide usable container config.

### OneCLI Client Boundary

Create a small host-side OneCLI client in Pynchy rather than depending on the Node SDK. The official Python SDK is documented as "coming soon", while the OneCLI API exposes the required orchestrator endpoints directly.

The client handles:

- `GET /v1/container-config?agent=<identifier>`;
- agent lookup/create for workspace identifiers;
- writing CA certificates and credential stubs to Pynchy-managed per-group directories;
- project header support when an org API key is paired with a project id.

The client must never request or expose secret values.

### Workspace Identity

Each Pynchy workspace maps to a stable OneCLI agent identifier:

```text
pynchy-<normalized-workspace-folder>
```

The normalized identifier must satisfy OneCLI's lowercase alphanumeric-plus-hyphen constraints. When an agent does not exist, Pynchy creates it if the configured API key can do so. If creation fails because the key lacks permission or OneCLI rejects the identifier, Pynchy fails with an actionable error that tells the operator to create the agent in OneCLI.

### Main Agent Containers

Before building final runtime args, Pynchy fetches OneCLI container config for the workspace's agent identifier. It then:

- adds OneCLI proxy/CA env vars to the per-group env file alongside non-secret values;
- writes the CA certificate into `data/onecli/{group}/ca/`;
- writes returned credential stubs under per-group state paths mounted into the container;
- mounts those paths read-only unless the target tool explicitly needs to refresh a local placeholder file.

When OneCLI is enabled, raw `GH_TOKEN` stops being written to agent env files. GitHub access is handled through OneCLI where possible, preferably with GitHub App or OAuth connections and OneCLI gateway injection for API and git-over-HTTPS calls.

### MCP Sidecars

For MCP servers that require credentials, add an explicit opt-in field to their config, for example:

```toml
[mcp.gmail]
type = "docker"
onecli = true
onecli_agent = "workspace"
```

When enabled, Docker MCP containers receive the OneCLI proxy env and CA mounts. MCP-specific credential files are created with `onecli-managed` placeholders, following OneCLI's credential-stub guidance. Existing `env_forward` remains available only for non-secret compatibility; docs mark it as a legacy/native credential path when OneCLI is enabled.

Script MCPs run on the host, so they are a different risk class. If they need OneCLI, Pynchy sets proxy/CA env vars for the subprocess, but host subprocesses already have ambient host access. That limitation is documented instead of hidden.

### No Generated Secret MCP

The first phase does not add a Pynchy-generated MCP. OneCLI already exposes the gateway skill and credential-stub APIs that cover the setup surface without introducing another tool protocol.

A future informational/control MCP is acceptable only if it wraps OneCLI workflow helpers:

- list configured OneCLI status for the workspace;
- surface connect URLs or setup errors;
- show which stubs were installed;
- report pending approval status from OneCLI APIs.

It must not include any tool that returns raw credentials, secret values, or decrypted token material.

### Failure Behavior

With `fail_closed = true`, Pynchy refuses to spawn a container when OneCLI is enabled but:

- the API key is missing;
- `/v1/container-config` is unreachable;
- OneCLI reports the agent does not exist and Pynchy cannot create it;
- the CA certificate or required stub files cannot be written.

With `fail_closed = false`, Pynchy logs a warning and falls back to the current native credential behavior. This mode exists for migration only, and docs label it as a weaker native credential path.

### Documentation

Update:

- `docs/architecture/security.md` to describe OneCLI as the preferred non-LLM credential boundary;
- `docs/architecture/container-isolation.md` to document proxy env vars, CA/stub mounts, and the no-raw-secret invariant;
- `docs/usage/mcp.md` to recommend OneCLI credential stubs over secret `env_forward`; <!-- pragma: allowlist secret - descriptive text, not a value -->
- `config-examples/config.toml.EXAMPLE` with the `[onecli]` section and migration notes.

### Testing

Unit tests cover:

- config parsing and disabled/default behavior;
- OneCLI client URL construction, auth headers, project headers, and error mapping;
- workspace identifier normalization;
- env-file generation with OneCLI enabled, including removal of raw `GH_TOKEN`;
- mount generation for CA and stub paths;
- MCP Docker env/mount generation when OneCLI is enabled;
- fail-closed versus migration fallback behavior.

Integration tests can use an in-process fake HTTP server for OneCLI responses. No real OneCLI service or real secrets are required for the default test suite.

## Decisions

- Pynchy auto-creates missing OneCLI agents when the configured API key permits it.
- `fail_closed` defaults to `true` whenever OneCLI is enabled. The native fallback exists only behind an explicit `fail_closed = false` migration setting.
- Pynchy keeps its LiteLLM/builtin LLM gateway independent in this phase. Routing model-provider traffic through OneCLI can be a separate follow-up once the non-LLM credential boundary is stable.

## References

- OneCLI `GET /v1/container-config`: https://onecli.sh/docs/api-reference/agent-setup/get-container-configuration.md
- OneCLI gateway skill endpoint: https://onecli.sh/docs/api-reference/agent-setup/get-the-gateway-skill.md
- OneCLI credential stubs: https://onecli.sh/docs/guides/credential-stubs/general-app.md
- OneCLI Node SDK container config behavior: https://onecli.sh/docs/sdks/node.md
