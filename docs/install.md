# Installation Guide

This guide walks through installing Pynchy on macOS or Linux, for both desktop and headless server deployments.

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
```

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

Enable WhatsApp plugin via config-managed plugins:

```toml
[plugins.whatsapp]
repo = "crypdick/pynchy-plugin-whatsapp"
ref = "main"
enabled = true
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
ui_password = "your-ui-password-here"
```

Then configure your models and API keys in `litellm_config.yaml` (see `litellm_config.yaml.example` for a starting point):

```bash
cp litellm_config.yaml.example litellm_config.yaml
# Edit litellm_config.yaml with your model providers and API keys
```

Pynchy starts the LiteLLM container automatically on boot. The admin UI is available at `http://localhost:4000/ui` (login with the `ui_username`/`ui_password` you configured).

### 3. Build Container Image

```bash
./container/build.sh                         # Build the agent container image
```

### 4. Authenticate WhatsApp

```bash
uv run pynchy-whatsapp-auth                 # Authenticate WhatsApp (scan QR code)
```

1. Open WhatsApp on your phone
2. Go to **Settings > Linked Devices > Link a Device**
3. Scan the QR code displayed in the terminal
4. Wait for "Successfully authenticated" before pressing Ctrl+C

### 5. Run Pynchy

```bash
uv run pynchy                                # Start Pynchy
```

On first run, Pynchy will:
- Create a private WhatsApp group for your god channel (admin control)
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
sg docker -c './container/build.sh'
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

Pynchy auto-discovers this token at startup and injects it into the LiteLLM container as `PYNCHY_ANTHROPIC_TOKEN` — no config.toml changes needed.

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
- Create a private WhatsApp group for your god channel
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

### 8. Deploying Updates

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

Then rebuild: `./container/build.sh`

### Port 8484 not reachable over Tailscale

- Verify Tailscale is connected: `tailscale status`
- The HTTP server binds to `0.0.0.0:8484` by default, which is accessible over Tailscale without any additional configuration
- Check firewall rules if on a cloud provider

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

For development and testing workflow, see `.claude/development.md` in the repository.
