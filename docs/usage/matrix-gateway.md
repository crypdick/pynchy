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
4. Approve the resulting external-send request with Pynchy's normal approval command.

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
the new **Pynchy communications gateway** device in Element before relying on it for
encrypted rooms. It receives new room keys under the room's encryption policy; it does
not recover old encrypted history automatically.

## Pynchy configuration

Select the tool only in the profile trusted with the full private inbox:

```toml
[tools.matrix-gateway]
type = "mcp"
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.matrix-gateway.mcp]
runtime = "script"
command = "uv"
args = ["run", "python", "-m", "pynchy.plugins.integrations.matrix_gateway", "--port", "{port}"]
port = 8476
transport = "streamable_http"

[profiles.personal-communications]
tools = ["matrix-gateway"]
contains_secrets = true
```

The tools are `matrix_list_chats`, `matrix_list_messages`, and
`matrix_send_message`. The first two are read-only; the send tool is approval-gated.
