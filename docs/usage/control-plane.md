# Control Plane Access

Pynchy exposes operational status, chat history, event streaming, canary evidence,
and deployment actions through one HTTP control plane. Use this guide to connect a
local or remote TUI without exposing those operations to unauthenticated clients.

## Local access

The default server starts two listeners:

- `data/pynchy.sock`, a Unix socket with mode `0600`; and
- `127.0.0.1:8484`, a loopback TCP fallback for platforms and clients that cannot
  use Unix sockets.

`uv run pynchy --tui` and `uv run pynchy doctor` prefer the Unix socket when it
exists, then fall back to loopback TCP. Pass `--socket <path>` to select a custom
socket.

The Unix socket relies on filesystem permissions and accepts local control requests
without a bearer token. Loopback TCP also accepts local requests without a token
until either remote-access option gets enabled.

## Readiness and operational status

`GET /health` returns only `{"status": "ok"}`. It stays unauthenticated so a local
service manager or external load balancer can perform a readiness probe without
receiving repository, channel, capability, or credential details.

Use the authenticated `/status`, `/capabilities`, `/canaries/*`, and `/api/*`
routes for operational details. A remote posture requires authentication for every
TCP route except `/health`, including unknown paths.

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
Keep the token out of `config.toml`, shell history, URLs, and query strings. A
client can read a copied mode-`0600` token with `--token-file`; otherwise it reads
`PYNCHY_CONTROL_TOKEN` and then `data/control-plane.token`.

## Enable remote TUI access

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

Transfer the token to the client through an authenticated secret-sharing channel,
store it in a mode-`0600` file, then connect:

```bash
uv run pynchy --token-file ~/.config/pynchy/control-plane.token \
  --tui --host your-server:8484
```

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

To call `/deploy` from a remote automation client, send the token in the
`Authorization: Bearer <token>` header. Never put it in the URL. The client must
handle `401`, `403`, and `429` as terminal policy responses rather than retrying
without correction.

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
