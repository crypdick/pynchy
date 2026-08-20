# Proton Mail

Give a workspace read and send access to Proton Mail through a host-side MCP
server. The server connects directly to local Proton Mail Bridge IMAP and SMTP
listeners and never gives a workspace the Bridge app password.

## Prerequisites

- Proton Mail Bridge is running on the Pynchy host with its IMAP and SMTP
  listeners bound to `127.0.0.1`. Bridge defaults to ports `1143` (IMAP) and
  `1025` (SMTP).
- The Bridge account's **app password** is available to a host-local command.
  The command must print only the password to stdout and must be readable only
  by the Pynchy host user. Do not put the password in `pynchy.toml`, source
  control, or a workspace environment.

Bridge's app password is separate from the Proton account password. On macOS,
use a Keychain-backed command after explicitly authorizing the command in the
logged-in graphical session. On Linux, use the host's secret service or another
host-local secret manager. Pynchy executes the configured command as argv, not
through a shell.

For K3s, build and publish the pinned Bridge image with
`.github/workflows/proton-bridge-image.yml`, then add one Bridge sidecar to the
Pynchy Pod in the deployment-specific Kustomize overlay. All authorized
workspaces share that host-side Bridge through the Proton Mail MCP server; do
not run one Bridge container per workspace. Persist `/home/bridge`, and mount
the generated Bridge app password from a Kubernetes Secret only into the
`pynchy` container. Account login remains a one-time interactive operator step.

## Configuration

Configure the direct Bridge identity and password command in the host-only MCP
configuration, then select the tool in a profile:

```toml
[tools.proton-mail]
type = "mcp"
skills = ["reading-proton-email"]
required_env = [
  "PYNCHY_PROTON_BRIDGE_USERNAME",
  "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND",
]
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.proton-mail.mcp]
runtime = "script"
command = "uv"
args = ["run", "python", "-m", "pynchy.plugins.integrations.proton_mail", "--port", "{port}"]
port = 8475
transport = "streamable_http"

[profiles.mail-research]
tools = ["proton-mail"]

[workspaces.mail-research]
profiles = ["mail-research"]
```

Set the declared variables in the Pynchy host process:

```dotenv
PYNCHY_PROTON_BRIDGE_USERNAME=you@example.com
PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND=/path/to/read-bridge-app-password
```

Add `PYNCHY_PROTON_BRIDGE_IMAP_PORT` and
`PYNCHY_PROTON_BRIDGE_SMTP_PORT` to `optional_env` only when Bridge uses
non-default ports.

The password command lets the host use its existing secret store without
passing a Bridge credential into agent containers or embedding it in TOML.
Pynchy sends these variables only to the Proton Mail MCP subprocess. Selecting
the tool also installs its companion reading skill. See
[Tool access and secrets](../usage/tool-access.md).

## Available tools

- `proton_list_mailboxes` lists mailboxes as `{name, mailbox}`. `name` is for
  display; pass the returned `mailbox` identifier to the other tools, including
  for internationalized mailbox names.
- `proton_list_mail` lists message metadata. It returns a `message_id`, not an
  IMAP UID, because Proton Bridge UIDs are not stable across connections.
- `proton_read_mail` fetches by `message_id` and uses a readonly mailbox plus
  `BODY.PEEK`, so it does not alter the message's read/unread state.
- `proton_send_mail` sends a plain-text message from the Bridge identity. It
  accepts `to`, `subject`, and `body` and returns the generated `message_id`.
- `proton_delete_mail` permanently removes a message by `message_id` from a
  selected mailbox.

Mail content arrives from outside the workspace and mail delivery can send data
to external recipients. Pynchy therefore marks this tool as a public source,
secret data, public sink, and dangerous write. Sending or deletion follows the
normal human-approval flow; the Bridge credential remains on the host.
