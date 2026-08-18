# Control Plane Access

Pynchy exposes operational status, capability snapshots, canary evidence, and
deployment actions through one HTTP control plane. Use this guide to inspect a
local or remote service without exposing those operations to unauthenticated clients.

## Local access

The default server starts two listeners:

- `data/pynchy.sock`, a Unix socket with mode `0600`; and
- `127.0.0.1:8484`, a loopback TCP fallback for platforms and clients that cannot
  use Unix sockets.

`uv run pynchy status`, `uv run pynchy deploy`, and `uv run pynchy doctor`
prefer the Unix socket when it exists, then fall back to loopback TCP. Pass
`--socket <path>` before the subcommand to select a custom socket.

The Unix socket relies on filesystem permissions and accepts local control requests
without a bearer token. Loopback TCP also accepts local requests without a token
until either remote-access option gets enabled.

## Send synthetic Discord canary input

Use the local control socket to exercise a configured Discord workspace through
the same outbound delivery, Discord gateway, and user-message ingestion paths as
normal operation:

```bash
curl --silent --show-error --unix-socket data/pynchy.sock \
  --header 'Content-Type: application/json' \
  --data '{"jid":"discord:channel:<channel-id>","content":"Reply with the available native tools."}' \
  http://localhost/canaries/messages
```

Pynchy sends the request from its existing Discord bot account with a visible
`🦜` prefix. The Discord adapter removes that prefix and records the inbound
message as user input with `synthetic_user_input = true` metadata. The agent sees
only the supplied content. This route accepts Discord channel JIDs only and uses
the same control-plane authentication policy as other `/canaries/*` routes.

## Readiness and operational status

`GET /health` returns only `{"status": "ok"}`. It stays unauthenticated so a local
service manager or external load balancer can perform a readiness probe without
receiving repository, channel, capability, or credential details.

Use the authenticated `/status`, `/capabilities`, `/actions`, `/work-items`, and
`/canaries/*` routes for operational details. `/actions` exposes external-write
state without draft payloads; see [Action coverage](../architecture/action-coverage.md#transactional-external-actions)
for its lifecycle. A remote posture requires authentication for every TCP route
except `/health` and exact plugin-registered webhook POST paths, including unknown
paths.

Use the bounded local clients instead of constructing HTTP requests by hand:

```bash
uv run pynchy status
uv run pynchy deploy
```

## Bootstrap a bearer token

Create a random token without printing it:

```bash
uv run pynchy control-plane bootstrap
```

Pynchy writes `data/control-plane.token` with mode `0600` by default. The command
refuses to overwrite an existing token. Rotate it explicitly when every remote
client can receive the replacement:

```bash
uv run pynchy control-plane bootstrap --rotate
```

The server reads `PYNCHY_CONTROL_TOKEN` first, then the configured token file.
Keep the token out of `pynchy.toml`, shell history, URLs, and query strings. A
client can read a copied mode-`0600` token with `--token-file`; otherwise it reads
`PYNCHY_CONTROL_TOKEN` and then `data/control-plane.token`.

## Enable remote diagnostic access

Remote access needs both an explicit public bind and the bearer token:

```toml
[server]
host = "0.0.0.0"
allow_public_bind = true
allow_remote_deploy = false
```

Pynchy refuses a non-loopback bind unless `allow_public_bind = true`. It also
refuses either remote-access option when no bearer token exists or when the token
contains fewer than 32 bytes. Network ACLs and firewalls remain useful defense in
depth, but they do not replace application authentication.

Transfer the token to diagnostic clients through an authenticated secret-sharing
channel and store it in a mode-`0600` file. The control-plane commands accept
`--token-file` and `--host` before the subcommand:

```bash
uv run pynchy --host pynchy.example:8484 --token-file ./pynchy.token status
```

HTTP clients send the same value in the `Authorization: Bearer <token>` header.

Pynchy compares bearer tokens without timing-sensitive string equality. It applies
a per-client fixed-window request limit before authentication so invalid tokens also
consume the request budget. Tune the defaults only when the client workload needs it:

```toml
[server]
rate_limit_requests = 120
rate_limit_window_seconds = 60
```

Policy decisions produce structured logs and durable `security_audit` rows without
recording the bearer token.

## Provider-authenticated webhooks

Plugin webhook routes use exact paths shaped as
`POST /webhooks/<provider>/<route>`. Those POST requests do not carry the Pynchy
control-plane bearer token because an external provider cannot know it. The route
plugin must authenticate the provider request from its raw body and headers before
parsing it. All other methods on the same path, unknown paths, and every normal
control-plane endpoint retain the bearer-token policy.

The host applies the global unauthenticated-client rate limit first, then a
route-specific body-size and rate limit. It accepts a configured route only when
its secret environment variable exists and every fixed or provider-derived
workspace target passes startup validation. Routes cannot target admin workspaces
unless the plugin establishes a trusted source policy and explicitly opts in. A
provider-derived route may opt into declared admin candidates only when its
source-trust policy satisfies the admin clean room. Schema-valid
authenticated deliveries are deduplicated and durably admitted before the
provider receives `200`. A provider may return `WebhookDiscard` after
authentication to receive `204` without a durable receipt or host effect. Routes
declare whether authenticated provider context remains a public source.
Public-source routes fence it and start the agent invocation corruption-tainted;
trusted routes retain provenance without that taint. Routes can also emit
deterministic host notifications without an agent run. Provider input cannot
bypass a subsystem's explicit authorization gates.

See [Linear](../integrations/linear.md#receive-linear-callbacks) for agent-task
callbacks and [GitHub](../integrations/github.md) for direct PR notifications.

## Enable remote deployment separately

A public bind does not grant remote deployment. TCP callers receive `403` unless
`allow_remote_deploy = true`, even when the TCP peer appears to come from loopback;
that rule prevents an SSH tunnel or reverse proxy from silently turning a remote
request into a trusted local deployment. Keep this stronger posture unless remote
deployment provides concrete operational value:

```toml
[server]
host = "0.0.0.0"
allow_public_bind = true
allow_remote_deploy = true
```

`allow_remote_deploy = true` also requires bearer authentication, even with a
loopback bind. Local callers can use the permission-restricted Unix socket without
enabling remote deployment.

To call `/deploy` from a remote automation client, use the authenticated CLI
form above with the `deploy` subcommand, or send the token in the
`Authorization: Bearer <token>` header. Never put it in the URL. The client
must handle `401`, `403`, and `429` as terminal policy responses rather than
retrying without correction.

## Configuration reference

| Key | Default | Purpose |
| --- | --- | --- |
| `host` | `127.0.0.1` | TCP bind address |
| `port` | `8484` | TCP fallback port |
| `unix_socket` | `data/pynchy.sock` | Preferred local control socket path |
| `allow_public_bind` | `false` | Permit a non-loopback TCP listener |
| `allow_remote_deploy` | `false` | Permit `/deploy` over TCP |
| `auth_token_env` | `PYNCHY_CONTROL_TOKEN` | Server environment variable that carries the bearer token |
| `auth_token_file` | `data/control-plane.token` | Server-side fallback token file |
| `rate_limit_requests` | `120` | Requests allowed per client and window |
| `rate_limit_window_seconds` | `60` | Fixed-window duration |

<!-- Source of truth: ServerConfig in src/pynchy/config/server.py and
http_control.py in src/pynchy/host/orchestrator/. Keep defaults and policy in sync. -->
