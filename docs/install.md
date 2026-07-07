# Installation Guide

Install Pynchy on macOS or Linux — desktop or headless server.

## Prerequisites

### Required Software

- **macOS or Linux** (tested on macOS 14+ and Ubuntu 24.04)
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** - Python package manager
- **LLM API key** - OpenAI by default, or another provider configured through LiteLLM
- **Temporal service** reachable from the Pynchy host for scheduled agent tasks
  (default: `localhost:7233`)
- **Container runtime:**
  - macOS: [Apple Container](https://github.com/apple/container) (preferred) or [Docker Desktop](https://docker.com/products/docker-desktop)
  - Linux: [Docker](https://docs.docker.com/engine/install/)

### System Dependencies

**macOS:**
```bash
brew install libmagic              # Required by neonize (WhatsApp) for MIME detection
brew install container             # Apple Container (recommended) — or install Docker Desktop
brew install temporal              # Scheduler service for agent tasks
brew services start container
container system kernel set --recommended
```

If you enable the LiteLLM gateway, also install and start Docker Desktop or
Colima. The agent runtime can use Apple Container, but the gateway currently
uses Docker networking.

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install libmagic1     # Required by neonize (WhatsApp) for MIME detection
# Install Docker + BuildKit - https://docs.docker.com/engine/install/
sudo apt-get install docker-buildx # Required for container image builds
```

**Linux (Fedora/RHEL):**
```bash
sudo dnf install file-libs         # Required by neonize (WhatsApp) for MIME detection
# Install Docker + BuildKit - https://docs.docker.com/engine/install/
sudo dnf install docker-buildx-plugin  # Required for container image builds
```

> **Note:** On macOS, if Apple Container is not installed, Pynchy automatically falls back to Docker.

---

## Installation Steps

### 1. Clone and Install Dependencies

```bash
git clone https://github.com/crypdick/pynchy.git
cd pynchy
uv sync                                      # Install Python dependencies
```

### 2. Configure

To customize configuration, copy the example and edit it:

```bash
cp config-examples/config.toml.EXAMPLE config.toml
# Edit config.toml with your preferred settings
```

Enable WhatsApp (requires optional dependency):

```bash
uv sync --extra whatsapp
```

Common configurations:

- **OpenAI API key:** Set `[secrets].openai_api_key`, or reference `OPENAI_API_KEY` from `litellm_config.yaml`
- **Anthropic API key:** Set `[secrets].anthropic_api_key`, or add an Anthropic API-key deployment to `litellm_config.yaml`
- **Temporal scheduler:** Run a Temporal service and set `[scheduler].temporal_address` if it does not listen on `localhost:7233`. For a single-host macOS service, use the `launchd/com.pynchy.temporal.plist` template and back up `data/temporal.db`; see [Scheduled Tasks](usage/scheduled-tasks.md#single-host-macos-service).
- **Claude SDK core:** Set `[agent] core = "claude"` and provide a valid Anthropic API key; Claude Code OAuth tokens are not supported as provider credentials.
- **Codex CLI core:** Configure a Codex-capable model in `litellm_config.yaml`, then set `[agent] core = "codex"` and `[agent] model` to that LiteLLM `model_name`. Codex model traffic routes through the same gateway as the OpenAI core.

#### LiteLLM Gateway (recommended)

Pynchy uses a [LiteLLM](https://docs.litellm.ai/) proxy as the LLM gateway. This runs in a Docker container and handles model routing, load balancing, and credential isolation — containers never see real API keys.

To enable it, add these settings to your `config.toml`:

```toml
[gateway]
litellm_config = "litellm_config.yaml"
port = 4000
master_key = "your-master-key-here"    # required — used for LiteLLM UI and API auth

# Optional: LiteLLM admin UI credentials
ui_username = "admin"
ui_password = "your-ui-password-here"    # pragma: allowlist secret
```

Then configure your models and API keys in `litellm_config.yaml` (see `config-examples/litellm_config.yaml.EXAMPLE` for a starting point):

```bash
cp config-examples/litellm_config.yaml.EXAMPLE litellm_config.yaml
# Edit litellm_config.yaml with your model providers and API keys
```

Pynchy starts the LiteLLM container automatically on boot. The admin UI is available at `http://localhost:4000/ui` (login with the `ui_username`/`ui_password` you configured).

To use the Codex CLI core, add a model route that Codex can request. LiteLLM's
OpenAI provider supports Codex models through the Responses API, and LiteLLM's
ChatGPT Subscription provider can also expose subscription-backed Codex models.
Keep those provider credentials in `litellm_config.yaml` / `.env`; do not run
`codex login` on the Pynchy host for container auth.

For ChatGPT Subscription routing, the model names must match LiteLLM's
`chatgpt/` provider route:

```toml
[agent]
core = "codex"
model = "chatgpt/gpt-5.3-codex"
```

#### MCP Server Access (optional)

To give agents access to external MCP tool servers (e.g., Playwright for web browsing), add definitions to `config.toml`:

```toml
[mcp_servers.playwright]
type = "docker"
image = "mcp/playwright:latest"
args = ["--headless", "--port", "8931", "--host", "0.0.0.0"]
port = 8931
transport = "sse"
idle_timeout = 600

[workspaces.my-workspace]
mcp_servers = ["playwright"]

# Optional: per-workspace restrictions (passed as Docker flags)
[workspaces.my-workspace.mcp.playwright]
allowed-origins = "github.com;stackoverflow.com"
```

Docker MCP containers start on-demand and stop after `idle_timeout`. See [MCP servers](usage/mcp.md) for configuration details.

### 3. Build Container Image

```bash
./src/pynchy/agent/build.sh                  # Build the agent container image
```

### 4. Migrate an Existing Install

For a migration, stop the old service before copying files. Copy the persistent
state into the same relative paths in the new checkout:

- `data/`
- `config.toml`
- `litellm_config.yaml`
- `.env`, if it stores gateway, channel, or model-provider secrets

Do not copy `data/deploy_continuation.json`. It only tracks an in-progress
deploy and can trigger a rollback to an old commit on the new host.

If `data/neonize.db` comes across, skip WhatsApp QR authentication unless the
session has expired. Do not blindly transplant `data/litellm/postgres` between
Linux Docker and macOS Apple Container deployments; let the gateway recreate
its database from config unless you explicitly need LiteLLM internal history.

The migrated `data/` directory can also contain runtime-only host state such as
`data/worktrees/`, `data/repos/`, and old `messages.db` rows. If startup hangs
on `git fetch` or repo-access workspaces because the new host cannot reach
GitHub over SSH yet, move those runtime directories aside or temporarily remove
the affected `repo_access` entries from `config.toml`. Old message history is
useful but not required for a successful cutover; prioritize one healthy service
instance over perfectly preserving historical rows.

If you keep migration safety copies under `data/migration-backups/`, Pynchy
prunes them after a deploy restart completes successfully and keeps the three
newest backup directories. To inspect or run the same cleanup manually, use:

```bash
uv run pynchy prune-migration-backups
uv run pynchy prune-migration-backups --keep 2 --apply
```

The command only prunes direct child directories of `data/migration-backups/`;
it ignores files and symlinks.

### 5. Authenticate WhatsApp

```bash
uv run pynchy-whatsapp-auth                 # Authenticate WhatsApp (scan QR code)
```

1. Open WhatsApp on your phone
2. Go to **Settings > Linked Devices > Link a Device**
3. Scan the QR code displayed in the terminal
4. Wait for "Successfully authenticated" before pressing Ctrl+C

### 6. Run Pynchy

```bash
uv run pynchy                                # Start Pynchy
```

On first run, Pynchy will:

- Create a private WhatsApp group for your admin channel (admin control)
- Set up local directories for group isolation
- Connect to WhatsApp and start listening for messages

---

## Headless Server Deployment

Step-by-step guide to deploying Pynchy on a headless Linux server with systemd, accessible over Tailscale.

For macOS desktop setup, see the [Installation Steps](#installation-steps) above.

### Prerequisites

On the server:

- Ubuntu/Debian Linux (tested on Ubuntu 24.04)
- [Tailscale](https://tailscale.com/download) connected to your tailnet
- An OpenAI API key, or another provider API key configured through LiteLLM
- A phone with WhatsApp (for QR code authentication)

On your local machine (for remote setup):

- SSH access to the server (Tailscale SSH or standard)
- [GitHub CLI](https://cli.github.com/) authenticated (`gh auth login`)

### 1. Install Server Dependencies

SSH into your server and install the required packages:

```bash
# System packages
sudo apt-get update && sudo apt-get install -y docker.io docker-buildx sqlite3
sudo usermod -aG docker $USER
# Log out and back in, or use `sg docker -c "docker ps"` to test

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env  # or restart your shell

# GitHub CLI (for cloning private repos)
sudo apt-get install -y gh

```

### 2. Clone and Build

```bash
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
git clone git@github.com:crypdick/pynchy.git ~/src/pynchy
cd ~/src/pynchy

# Install Python dependencies
uv sync

# Build the agent container image
sg docker -c './src/pynchy/agent/build.sh'
```

### 3. Authenticate WhatsApp

This step requires scanning a QR code with your phone. The QR code renders as Unicode text and pipes cleanly over SSH.

```bash
cd ~/src/pynchy
uv run pynchy-whatsapp-auth
```

1. Open WhatsApp on your phone
2. Go to **Settings > Linked Devices > Link a Device**
3. Scan the QR code displayed in the terminal

Wait for "Successfully authenticated" before pressing Ctrl+C.

### 4. Configure LLM Credentials

Pynchy defaults to the OpenAI agent core. Put provider credentials in `.env`
and reference them from `litellm_config.yaml`:

```bash
cd ~/src/pynchy
cp config-examples/config.toml.EXAMPLE config.toml
cp config-examples/litellm_config.yaml.EXAMPLE litellm_config.yaml
cat >> .env << 'EOF'
OPENAI_API_KEY=your-openai-api-key
GATEWAY__MASTER_KEY=change-this-master-key
EOF
```

> **Warning:** Without credentials, Pynchy will start and connect to WhatsApp, but all messages to the agent will fail. The boot notification will warn you if credentials are missing.

### 5. First Run

```bash
cd ~/src/pynchy
uv run pynchy
```

On first run, Pynchy will:

- Create a private WhatsApp group for your admin channel
- Install a systemd user service (`~/.config/systemd/user/pynchy.service`)
- Enable the service for auto-start on boot
- Enable user lingering (so the service runs without an active login session)

Verify it's working, then press Ctrl+C.

### 6. Start as a Service

The first run already installed and enabled the systemd service. Start it now:

```bash
systemctl --user start pynchy
```

For a reference unit file template, see `config-examples/pynchy.service.EXAMPLE`.

Check status:

```bash
systemctl --user status pynchy
```

View logs:

```bash
journalctl --user -u pynchy -f
```

The service auto-restarts on crashes (`RestartSec=10`) and starts on boot.

### 7. Connect the TUI (optional)

From any machine on your Tailscale network:

```bash
uv run pynchy --tui --host your-server:8484
```

Replace `your-server` with the Tailscale hostname of your server (visible in `tailscale status`).

### 8. Daily Maintenance (recommended, Linux only)

Set up automatic security updates and a daily reboot to clear zombie processes and keep the server fresh. Pynchy auto-starts on boot.

**Automatic security updates** — `unattended-upgrades` is usually pre-installed on Ubuntu. Configure it to apply updates daily and reboot when kernel updates are pending:

```bash
sudo cp config-examples/20auto-upgrades.EXAMPLE       /etc/apt/apt.conf.d/20auto-upgrades
sudo cp config-examples/50unattended-upgrades.EXAMPLE  /etc/apt/apt.conf.d/50unattended-upgrades
```

**Daily reboot + Docker cleanup** — a systemd timer that prunes Docker resources older than 48 hours, then reboots the machine:

```bash
sudo cp config-examples/pynchy-maintenance.service /etc/systemd/system/
sudo cp config-examples/pynchy-maintenance.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pynchy-maintenance.timer
```

The timer fires at 4 AM daily (with up to 5 minutes of randomized jitter). Verify with `systemctl list-timers pynchy-maintenance*`.

> **Note:** Reference copies of all server configs are version controlled in `config-examples/`. The live copies on the server are the source of truth.

### 9. Deploying Updates

After pushing changes to the repo, trigger a remote deploy:

```bash
curl -X POST http://your-server:8484/deploy
```

This pulls the latest code, validates the import, and restarts the service. If the import fails, Pynchy automatically rolls back.

---

## Troubleshooting

### "No API credentials found" in boot message

Set `OPENAI_API_KEY` in `.env` and make sure `litellm_config.yaml` references it, or set `[secrets].openai_api_key` in `config.toml` for builtin gateway mode. Then restart:

```bash
systemctl --user restart pynchy
```

### WhatsApp QR code not scanning

- Ensure your phone and server can reach each other over the network (or use SSH tunneling)
- The QR code renders as Unicode text in the terminal — scan it directly from the SSH session
- If the QR code doesn't render properly, try a different terminal emulator

### Container build fails

**macOS:**

- Ensure Apple Container or Docker is running
- Check that you have the latest version: `brew upgrade container` or update Docker Desktop

**Linux:**

- Ensure Docker is running: `sudo systemctl start docker`
- Verify you're in the docker group: `groups | grep docker`
- If not, run `sudo usermod -aG docker $USER` and log out/in
- **"BuildKit is enabled but the buildx component is missing"**: Install the buildx plugin: `sudo apt-get install docker-buildx` (Debian/Ubuntu) or `sudo dnf install docker-buildx-plugin` (Fedora/RHEL). BuildKit is required for container builds.

Then rebuild: `./src/pynchy/agent/build.sh`

### Port 8484 not reachable over Tailscale

- Verify Tailscale is connected: `tailscale status`
<!-- Source of truth for the default port: ServerConfig.port in src/pynchy/config/models.py — keep the 8484 references in this file in sync. -->
- The HTTP server binds to `0.0.0.0:8484` by default, which is accessible over Tailscale without any additional configuration
- Check firewall rules if on a cloud provider

### macOS launchd service does not stay loaded

If `uv run pynchy` works manually but the LaunchAgent exits immediately:

- Validate the installed plist: `plutil -lint ~/Library/LaunchAgents/com.pynchy.plist`
- Confirm launchd is using resolved paths, not literal `$HOME` strings:
  `launchctl print gui/$(id -u)/com.pynchy`
- If the label is not loaded, stop the foreground process and bootstrap the
  LaunchAgent explicitly:
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pynchy.plist`
- Check `logs/pynchy.log` and `logs/pynchy.error.log` for application startup errors.

### Service won't start after reboot

Check that lingering is enabled: `loginctl show-user $USER | grep Linger`. If not, run `sudo loginctl enable-linger $USER`.

### WhatsApp disconnects

WhatsApp linked devices expire after ~30 days of inactivity. Re-run `uv run pynchy-whatsapp-auth` to re-authenticate, then restart the service.

### First run doesn't create systemd service

On first run, the systemd service is only created if you start Pynchy without the `--tui` flag. Run `uv run pynchy` (not `uv run pynchy --tui`) for the initial setup.

---

## Next Steps

After installation:

1. **Send a test message** - Message yourself in WhatsApp with `@Pynchy hello` to verify it's working
2. **Read the docs** - Understand the philosophy at [index.md](index.md) and architecture at [architecture/](architecture/index.md)
3. **Customize** - Tell Pynchy to add channels, integrations, or change behavior directly in the codebase
4. **Set up scheduled tasks** - Ask Pynchy to run recurring tasks: `@Pynchy send me a summary of Hacker News every morning at 9am`

For development and testing workflow, see `.claude/skills/pynchy-dev/SKILL.md` in the repository.
