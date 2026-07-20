# Matrix routed conversations

The Matrix gateway lets Pynchy operate selected bridged chats through the
Matrix account that owns them. Each configured room gets a durable agent
conversation and a human-readable Discord control thread beneath an explicit
parent workspace. The Matrix access token and encryption store remain on the
host.

The existing `pynchy-matrix-reader` remains separate and read-only. Its
`@pynchy` identity is appropriate for room-scoped agent access. This gateway
signs in as the bridge-owning human account and can send only through a routed,
approval-gated host action.

## Configure a connection and route

Declare the route-scoped tools in the parent workspace profile:

```toml
[tools.matrix_route_read]
type = "builtin"
name = "matrix_route_read"
public_source = true
secret_data = true
public_sink = false
dangerous_writes = false

[tools.matrix_route_send]
type = "builtin"
name = "matrix_route_send"
public_source = false
secret_data = false
public_sink = true
dangerous_writes = true

[profiles.personal-communications]
tools = ["matrix_route_read", "matrix_route_send"]

[workspaces.personal]
profiles = ["personal-communications"]
```

Configure one owner identity, its exact room endpoints, and top-level routes:

```toml
[connections.personal-chats]
type = "matrix"
expected_user_id = "@owner:matrix.example.com"
poll_interval_seconds = 5

[connections.personal-chats.route_defaults]
activation = "on_demand"
outbound = "approval_required"

[connections.personal-chats.chat.family]
room_id = "!immutable-room-id:matrix.example.com"
title = "Family"
expected_bridge = "WhatsApp"
require_active_portal = true

[routes.family]
source = "connection.matrix.personal-chats.chat.family"
workspace = "personal"
activation = "on_event"
outbound = "approval_required"
tools = ["matrix_route_read", "matrix_route_send"]
```

Connection and route names are operator-facing names. Renaming one creates a
new runtime or route identity; clean cutovers are intentional. A route must
reference an enabled endpoint and an explicit existing workspace. One endpoint
cannot feed two routes.

`activation = "on_event"` wakes the route's agent conversation when an eligible
message arrives. `activation = "on_demand"` records authenticated events and
advances the cursor without enqueueing agent work. `outbound = "read_only"`
rejects sends before an approval can be created. Route `tools` and capability
rules may only reduce the parent workspace's privileges. An `on_event` route
cannot target an admin workspace.

## Build and authenticate the gateway

Build on the Pynchy host:

```sh
cargo build --release --manifest-path tools/pynchy-matrix-gateway/Cargo.toml
```

Point the host service at the absolute binary path in its private environment
file:

```sh
PYNCHY_MATRIX_GATEWAY=/absolute/path/to/pynchy-matrix-gateway
```

Each connection has its own gateway state directory:

```text
<pynchy-project>/data/matrix-gateway/connection-<percent-encoded-connection-name>
```

For the `personal-chats` example, authenticate and verify the same store that
the runtime will use:

```sh
export PYNCHY_MATRIX_GATEWAY_DATA_DIR="$PWD/data/matrix-gateway/connection-personal-chats"
printf '%s' "$MATRIX_PASSWORD" | "$PYNCHY_MATRIX_GATEWAY" \
  login --homeserver https://matrix.example.com \
  --user @owner:matrix.example.com --password-stdin
"$PYNCHY_MATRIX_GATEWAY" verify --device EXISTING_ELEMENT_DEVICE_ID
```

Run these commands from the Pynchy project root, or replace `$PWD/data` with
the absolute configured data directory. Slashes, spaces, percent signs, and
other non-path-safe characters in a connection name are percent-encoded after
the fixed `connection-` prefix.

Never put the Matrix password in an argument, configuration file, or agent
prompt. Accept the verification request in an already trusted Element session,
compare the seven emojis, and type `confirm` into the running gateway command
only when they match. Verification permits future room-key sharing; it cannot
recover historical keys that were never backed up or forwarded.

## Inbound behavior

The runtime performs an initial sync before reporting ready, then persists one
cursor per named connection. It validates the configured room, owner, bridge,
and active-portal assertions before admitting a page. A missing or mismatched
assertion fails closed.

Only live, decrypted, original `m.room.message` events with `msgtype = "m.text"`
from someone other than the owner are eligible. Backfill, edits, reactions,
redactions, owner-authored messages, and unconfigured rooms do not wake an
agent. A live event that cannot be decrypted fails the page and retains the
previous cursor instead of pretending the room was empty.

Eligible event IDs are durable delivery identities. Replays do not create new
conversations or FIFO entries. Each route processes one claimed delivery at a
time; another route or conversation can run concurrently. Startup releases an
interrupted claim and wakes it again in its original FIFO position.

Matrix text remains untrusted even though the host authenticated the provider
delivery. It starts the routed turn with corruption and secret-source taint and
cannot invoke host commands embedded in its body.

## Route-scoped tools and approval

Generated conversation workspaces expose only the route restrictions inherited
from their parent. Matrix contributes:

- `matrix_route_read(limit)` reads the room bound to the active conversation.
- `matrix_route_send(body)` requests a send to that same bound room.

Neither tool accepts a room or destination argument. The host resolves the
active route, current control binding, connection-specific state directory, and
configured portal. Read results omit room IDs.

Every send requires human approval in the current Discord control thread. The
approval binds the exact connection, route, conversation, thread, room, portal
assertion, and final body. Approval replay re-resolves current workspace policy
and rechecks the live portal before the gateway writes. A route rename, control
replacement, policy denial, expired request, changed payload, or portal change
invalidates the pending action.

Human approval does not require a second conversation. Approve or deny in the
same control thread while the agent turn is active; Pynchy executes the control
message without adding it to agent input.

## Check readiness

Inspect the connection loop and route actions with:

```sh
pynchy doctor --workspace personal
curl http://localhost:8484/status
```

The protected status response includes each named connection runtime and its
current readiness. `pynchy doctor --workspace personal --json` reports route
action setup, policy, and gateway availability. A ready snapshot is diagnostic,
not authorization: dispatch and approved replay always enforce current policy
and live route assertions again.
