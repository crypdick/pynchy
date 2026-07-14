# Proton Mail

Give a workspace read-only access to Proton Mail through a host-side MCP server. The server runs `pm-cli` on the host, so the workspace container never receives a Proton Bridge password or host Keychain access.

## Prerequisites

Install and authenticate `pm-cli` with Proton Bridge on the Pynchy host. Confirm that the account works in the same macOS login session that runs Pynchy.

## Configuration

Set the host path to `pm-cli` in the MCP process environment, then select the tool in a profile:

```toml
[tools.proton-mail]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools.proton-mail.mcp]
runtime = "script"
command = "uv"
args = ["run", "python", "-m", "pynchy.plugins.integrations.proton_mail", "--port", "{port}"]
port = 8475
transport = "streamable_http"
env = { PYNCHY_PROTON_PM_CLI = "/path/to/pm-cli" }

[profiles.mail-research]
tools = ["proton-mail"]

[workspaces.mail-research]
profiles = ["mail-research"]
```

The built-in server provides mailbox listing, message listing, and message reading. Reading preserves the original read/unread state. It deliberately does not expose sending, deleting, or mailbox mutation.

Use `proton_list_mail` first, then pass the returned UID to `proton_read_mail`. Avoid guessing UIDs or relying on stale full-text search results.
