# Installation Guide

Install Pynchy on macOS or Linux — desktop or headless server.

## Prerequisites

### Required Software

- **macOS or Linux** (tested on macOS 14+ and Ubuntu 24.04)
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** - Python package manager
- **[Claude Code](https://claude.ai/download)** - AI development assistant
- **Container runtime:**
  - macOS: [Apple Container](https://github.com/apple/container) (preferred) or [Docker Desktop](https://docker.com/products/docker-desktop)
  - Linux: [Docker](https://docs.docker.com/engine/install/)

### System Dependencies

**macOS:**
```bash
brew install libmagic              # Required by neonize (WhatsApp) for MIME detection
brew install container             # Apple Container (recommended) — or install Docker Desktop
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

- **API key authentication:** Set `[secrets].anthropic_api_key` instead of Claude Code OAuth
- **OpenAI instead of Claude:** Set `[agent] core = "openai"` and `[secrets].openai_api_key`

> **Note:** For most desktop setups, you can skip this step and authenticate using Claude Code OAuth (see step 4 in Headless Server Deployment).

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
- [Node.js](https://nodejs.org/) 18+ (for installing Claude Code)
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

# Node.js + npm (for installing Claude Code)
sudo apt-get install -y npm
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

### 4. Authenticate Claude Code

Pynchy runs agents using the Claude Agent SDK, which requires Claude Code installed and authenticated. Pynchy auto-discovers credentials at startup — no manual config needed.

```bash
# Install Claude Code on the server
npm install -g @anthropic-ai/claude-code
```

**Pro/Max subscribers (recommended):** Generate a long-lived token (~1 year):

1. Run `claude setup-token` on the server
2. It prints a URL — paste it into a browser (can be on any machine)
3. Authorize in the browser and copy the code it gives you
4. Paste the code back into the `setup-token` prompt
5. Copy the long-lived token it outputs (starts with `sk-ant-oat01-...`)
6. Create the credentials file on the server:

```bash
mkdir -p ~/.claude
cat > ~/.claude/.credentials.json << 'EOF'
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-YOUR_TOKEN_HERE"}}
EOF
chmod 600 ~/.claude/.credentials.json
```

To route LLM requests through the LiteLLM gateway using this token, add it to `.env` as `CLAUDE_OAUTH_TOKEN=sk-ant-oat01-YOUR_TOKEN_HERE` and reference it in `litellm_config.yaml` (see `config-examples/litellm_config.yaml.EXAMPLE` for the OAuth entry). LiteLLM auto-detects the `sk-ant-oat*` prefix and handles auth headers.

**API key (pay-as-you-go):** Get a key from [console.anthropic.com](https://console.anthropic.com), then set it in `config.toml`:

```bash
cp ~/src/pynchy/config-examples/config.toml.EXAMPLE ~/src/pynchy/config.toml
# Set [secrets].anthropic_api_key in config.toml
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

Run `claude setup-token` on the server to generate a long-lived token, or set `[secrets].anthropic_api_key` in `config.toml`. Then restart: `systemctl --user restart pynchy`

### OAuth token expired (401 authentication_error)

Short-lived OAuth tokens from `claude` login expire every ~8 hours. Generate a long-lived token (~1 year) instead — follow the `claude setup-token` steps in [section 4](#4-authenticate-claude-code), then restart:

```bash
systemctl --user restart pynchy
```

### OAuth token rejected by Anthropic organization policy

If LiteLLM logs a 403 like `OAuth authentication is currently not allowed for
this organization`, the host migration worked but the Claude Code OAuth token
cannot be used as an Anthropic API credential for that organization. Use a
sanctioned Anthropic API key in `litellm_config.yaml` / `.env`, or switch the
active agent core and gateway route to a provider with a valid key. Do not
treat this as a WhatsApp/session migration failure.

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
- On macOS, if raw tailnet access to `:8484` is unreliable and you use Tailscale
  Serve, do not bind Serve and Pynchy to the same local port. Set Pynchy to a
  different local port, for example:

  ```toml
  [server]
  port = 8485
  ```

  Then expose the stable tailnet URL with:

  ```bash
  tailscale serve --bg --http 8484 8485
  ```

- Check firewall rules if on a cloud provider

### macOS launchd service does not stay loaded

If `uv run pynchy` works manually but the LaunchAgent exits immediately:

- Validate the installed plist: `plutil -lint ~/Library/LaunchAgents/com.pynchy.plist`
- Check for quarantine/provenance metadata and remove it if present:
  `xattr -l ~/Library/LaunchAgents/com.pynchy.plist` then
  `xattr -d com.apple.provenance ~/Library/LaunchAgents/com.pynchy.plist`
- Confirm launchd is using resolved paths, not literal `$HOME` strings:
  `launchctl print gui/$(id -u)/com.pynchy`
- If `launchctl print` briefly shows `/opt/homebrew/bin/uv run pynchy` and then
  the label disappears with empty logs, run the same command in a foreground
  shell or tmux to separate launchd issues from application startup issues.
  GitHub SSH hangs from migrated `repo_access` workspaces can look like service
  startup failures.

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
