# Local installation

Use this guide to install Pynchy on macOS or Linux for local operation.

## Prerequisites

- macOS or Linux (tested on macOS 14+ and Ubuntu 24.04)
- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- An LLM provider credential, routed directly or through LiteLLM
- A reachable Temporal service for scheduled work (default: `localhost:7233`)
- A container runtime: Apple Container or Docker on macOS; Docker on Linux

### System dependencies

On macOS:

```bash
brew install libmagic              # Needed only for WhatsApp
brew install container             # Preferred agent runtime; Docker also works
brew install temporal
brew services start container
container system kernel set --recommended
```

LiteLLM currently uses Docker networking, so install Docker Desktop or Colima
when you enable the LiteLLM gateway even if agents use Apple Container.

On Debian or Ubuntu:

```bash
sudo apt-get install libmagic1 docker-buildx
```

On Fedora or RHEL:

```bash
sudo dnf install file-libs docker-buildx-plugin
```

If Apple Container is unavailable on macOS, Pynchy falls back to Docker.

## Install and configure

```bash
git clone https://github.com/crypdick/pynchy.git
cd pynchy
uv sync
cp config-examples/config.toml.EXAMPLE config.toml
```

Install the WhatsApp extra only when you use WhatsApp:

```bash
uv sync --extra whatsapp
```

For Slack, Discord, or the local TUI, follow [Channels](../channels/index.md).

### Configure models and the gateway

Set provider credentials through `[secrets]` or reference environment variables
from `litellm_config.yaml`. To use LiteLLM, copy its example configuration:

```bash
cp config-examples/litellm_config.yaml.EXAMPLE litellm_config.yaml
```

Then configure the gateway in `config.toml`:

```toml
[gateway]
litellm_config = "litellm_config.yaml"
port = 4000
master_key = "your-master-key-here"
```

Pynchy starts LiteLLM on boot and keeps provider credentials outside agent
containers. Configure a model route before selecting the Codex core; then set
`[agent] default_core = "codex"` and select the route globally, in a profile,
or in a workspace. For MCP tools, see [MCP servers](../usage/mcp.md). For a
single-host macOS Temporal service, see [Scheduled tasks](../usage/scheduled-tasks.md#single-host-macos-service).

### Build and run

```bash
./src/pynchy/agent/build.sh
uv run pynchy
```

On its first run, Pynchy creates an admin workspace in the configured command
center when possible. Otherwise, it starts with a local TUI workspace.

### Authenticate WhatsApp

If you configured WhatsApp, link the device after installation:

```bash
uv run pynchy-whatsapp-auth
```

Open WhatsApp on your phone, go to **Settings → Linked Devices → Link a
Device**, and scan the terminal QR code. Wait for the authentication
confirmation before stopping the command.

For local speech in a Discord voice workspace, see [Local speech synthesis](../usage/host-capabilities/local-speech.md).
