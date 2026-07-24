# Local installation

Use this guide to install Pynchy on macOS or Linux for local operation.

## Prerequisites

- macOS or Linux (tested on macOS 14+ and Ubuntu 24.04)
- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- An LLM provider credential routed through LiteLLM
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
```

Install the WhatsApp extra only when you use WhatsApp:

```bash
uv sync --extra whatsapp
```

For Slack or Discord, follow [Channels](../channels/index.md).

### Check out personalization

Pynchy requires an independent personalization repository at the conventional
path. Clone an existing one:

```bash
git clone git@github.com:YOUR-ACCOUNT/pynchy-personalization.git \
  data/personalization
```

For a new deployment, start from `config-examples/personalization/`, create a
private repository, and then check it out at `data/personalization/`. See the
[personalization repository guide](../usage/personalization.md) for the full
layout and CI workflow.

Configure models in `data/personalization/litellm.yaml` and non-secret Pynchy
settings in `data/personalization/pynchy.toml`. Put provider keys and the
gateway key in the root `.env`:

```dotenv
OPENAI_API_KEY=sk-proj-...
GATEWAY__MASTER_KEY=replace-with-a-long-random-value
```

Validate the complete tree before starting:

```bash
uv run pynchy validate-personalization data/personalization
```

Pynchy starts LiteLLM on boot and keeps provider credentials outside agent
containers. Configure a model route before selecting the Codex core. For MCP
tools, see [MCP servers](../usage/mcp.md). For a single-host macOS Temporal
service, see [Scheduled tasks](../usage/scheduled-tasks.md#single-host-macos-service).

### Build and run

```bash
./src/pynchy/agent/build.sh
uv run pynchy
```

On its first run, Pynchy creates an admin workspace through the configured
command-center connection. Configure a creation-capable channel before starting
with an empty workspace database.

### Authenticate WhatsApp

If you configured WhatsApp, link the device after installation:

```bash
uv run pynchy-whatsapp-auth
```

Open WhatsApp on your phone, go to **Settings → Linked Devices → Link a
Device**, and scan the terminal QR code. Wait for the authentication
confirmation before stopping the command.

For local speech in a Discord voice workspace, see [Local speech synthesis](../usage/host-capabilities/local-speech.md).
