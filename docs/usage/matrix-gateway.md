# Matrix communications gateway

The Matrix communications gateway gives Pynchy a host-only view of the Matrix
account that owns bridged chats. It lets the assistant summarize conversations,
identify conversations that need a follow-up, and draft replies in Pynchy's normal
control chat. The agent never receives the Matrix access token.

The existing `pynchy-matrix-reader` remains separate and read-only. Do not replace
it with this gateway: its `@pynchy` identity is appropriate for room-scoped agent
access, while the gateway signs in as the bridge-owning human account.

## Outbound approval

`matrix_send_message` sends as the gateway owner. For a portal room, that means a
recipient sees the ordinary WhatsApp, Signal, Google Messages, or X account—not
Pynchy and not a relay label.

The tool is declared as a public sink with dangerous writes, so Pynchy's normal
approval gate stops every external send. The intended workflow is:

1. Ask Pynchy to review a conversation or prepare a reply.
2. Review or revise the draft in the chat where you normally talk to Pynchy.
3. Ask Pynchy to send the final version.
4. Select **Approve** on the resulting external-send request. Text-only channels show the matching approval command in the prompt.

No automatic external sends are enabled by this integration.

## Host setup

Build the gateway on the Pynchy host:

```sh
cargo build --release --manifest-path tools/pynchy-matrix-gateway/Cargo.toml
```

Point the host service at the compiled binary using its private environment file:

```sh
PYNCHY_MATRIX_GATEWAY=/absolute/path/to/pynchy-matrix-gateway
```

Log in once from an interactive host shell. Send the password only on standard input;
never put it in a command argument, configuration file, or agent prompt.

```sh
printf '%s' "$MATRIX_PASSWORD" | "$PYNCHY_MATRIX_GATEWAY" \
  login --homeserver https://matrix.example.com --user @owner:matrix.example.com --password-stdin
```

The session token, encrypted store, and store key live in
`~/.local/share/pynchy/matrix-gateway/` with private filesystem permissions. Verify
the new **Pynchy communications gateway** device before relying on encrypted rooms. The
gateway provides an interactive SAS command so the verification has a real second
endpoint; merely viewing the device in Element is not sufficient:

```sh
pynchy-matrix-gateway verify --device EXISTING_ELEMENT_DEVICE_ID
```

Accept the request in the already-trusted Element session, compare the seven emojis, and
type `confirm` into the still-running gateway command only when they match. The command
finishes with `{"status":"verified"}`. Verification allows the trusted client to share
future room keys; it does not guarantee recovery of old encrypted history that was never
backed up or forwarded to the gateway.

If a read finds only undecryptable encrypted events, Pynchy reports that gateway keys are
unavailable rather than incorrectly reporting an empty room.

## Pynchy configuration

These are native Pynchy tools, not a separately hosted remote MCP server. Select
them only in the profile trusted with the full private inbox:

```toml
[tools.matrix_list_chats]
type = "builtin"
name = "matrix_list_chats"
public_source = true
secret_data = true
public_sink = false
dangerous_writes = false

[tools.matrix_list_messages]
type = "builtin"
name = "matrix_list_messages"
public_source = true
secret_data = true
public_sink = false
dangerous_writes = false

[tools.matrix_send_message]
type = "builtin"
name = "matrix_send_message"
public_source = false
secret_data = true
public_sink = true
dangerous_writes = true

[profiles.personal-communications]
tools = ["matrix_list_chats", "matrix_list_messages", "matrix_send_message"]
contains_secrets = true
```

The tools are `matrix_list_chats`, `matrix_list_messages`, and
`matrix_send_message`. The first two are read-only. The send tool always stops at
Pynchy's human-approval boundary before the host gateway transmits it.

Matrix chat text is treated as an untrusted source. Reading it taints the agent turn
before any later external operation, so the existing approval boundary remains in
effect even if a bridged message tries to steer the agent.
