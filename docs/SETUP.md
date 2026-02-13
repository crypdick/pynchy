# Server Setup Guide

Step-by-step guide to deploying Pynchy on a headless Linux server. This covers the full path from a fresh machine to a running, auto-restarting service accessible over Tailscale.

For macOS desktop setup, run `claude` then `/setup` — Claude Code handles everything interactively.

---

## Prerequisites

On the server:
- Ubuntu/Debian Linux (tested on Ubuntu 24.04)
- [Tailscale](https://tailscale.com/download) connected to your tailnet
- [Node.js](https://nodejs.org/) 18+ (for installing Claude Code)
- A phone with WhatsApp (for QR code authentication)

On your local machine (for remote setup):
- SSH access to the server (Tailscale SSH or standard)
- [GitHub CLI](https://cli.github.com/) authenticated (`gh auth login`)

## 1. Install dependencies

SSH into your server and install the required packages:

```bash
# System packages
sudo apt-get update && sudo apt-get install -y docker.io sqlite3
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

## 2. Clone and build

```bash
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
git clone git@github.com:crypdick/pynchy.git ~/src/pynchy
cd ~/src/pynchy

# Install Python dependencies
uv sync

# Build the agent container image
sg docker -c './container/build.sh'
```

## 4. Authenticate WhatsApp

This step requires scanning a QR code with your phone. If you have physical access to the server, run directly. Otherwise, the QR code renders as Unicode text and pipes cleanly over SSH:

```bash
cd ~/src/pynchy
uv run pynchy auth
```

1. Open WhatsApp on your phone
2. Go to **Settings > Linked Devices > Link a Device**
3. Scan the QR code displayed in the terminal

Wait for "Successfully authenticated" before pressing Ctrl+C.

## 5. Install Claude Code and authenticate

Pynchy runs agents using the Claude Agent SDK, which requires Claude Code to be installed and authenticated.

```bash
# Install Claude Code on the server
npm install -g @anthropic-ai/claude-code
```

The easiest way to authenticate on a headless server is to copy credentials from a machine where you're already logged in:

```bash
# From your local machine (where claude is already authenticated):
ssh user@your-server "mkdir -p ~/.claude"
scp ~/.claude/.credentials.json user@your-server:~/.claude/.credentials.json
```

Verify it works on the server:

```bash
ssh your-server "claude -p 'say hello'"
```

**Alternative: API key instead of OAuth**

If you prefer using an API key directly, skip the `claude` authentication and create a `.env` file:

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > ~/src/pynchy/.env
```

> Without credentials, Pynchy will start and connect to WhatsApp, but all messages to the agent will fail. The boot notification will warn you if credentials are missing.

## 6. First run

```bash
cd ~/src/pynchy
uv run pynchy
```

On first run, Pynchy will:
- Create a private WhatsApp group for your main channel
- Install a systemd user service (`~/.config/systemd/user/pynchy.service`)
- Enable the service for auto-start on boot
- Enable user lingering (so the service runs without an active login session)

Verify it's working, then press Ctrl+C.

## 7. Start as a service

The first run already installed and enabled the systemd service. Start it:

```bash
systemctl --user start pynchy
```

Check status:

```bash
systemctl --user status pynchy
```

View logs:

```bash
journalctl --user -u pynchy -f
```

The service will auto-restart on crashes (`RestartSec=10`) and start on boot.

## 8. Connect the TUI (optional)

From any machine on your Tailscale network:

```bash
uv run pynchy --tui --host your-server:8484
```

Replace `your-server` with the Tailscale hostname of your server (visible in `tailscale status`).

## Deploying updates

After pushing changes to the repo, trigger a remote deploy:

```bash
curl -X POST http://your-server:8484/deploy
```

This pulls the latest code, validates the import, and restarts the service. If the import fails, it automatically rolls back.

## Troubleshooting

**"No API credentials found" in boot message**

Run `claude` on the server to authenticate, or set `ANTHROPIC_API_KEY` in `.env`. Then restart: `systemctl --user restart pynchy`

**Port 8484 not reachable over Tailscale**

Verify Tailscale is connected: `tailscale status`. The HTTP server binds to `0.0.0.0:8484` by default, which is accessible over Tailscale without any additional configuration.

**Service won't start after reboot**

Check that lingering is enabled: `loginctl show-user $USER | grep Linger`. If not, run `sudo loginctl enable-linger $USER`.

**WhatsApp disconnects**

WhatsApp linked devices expire after ~30 days of inactivity. Re-run `uv run pynchy auth` to re-authenticate, then restart the service.

**Container build fails**

Ensure Docker is running: `sudo systemctl start docker`. Then rebuild: `./container/build.sh`
