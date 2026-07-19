# Headless Linux server

Deploy Pynchy on a headless Linux server as a systemd user service. The examples
use Tailscale and WhatsApp; configure a different channel when WhatsApp does not
fit the deployment.

## Prerequisites

On the server, use Ubuntu or Debian with Docker, a reachable Temporal service,
and [Tailscale](https://tailscale.com/download) when you need remote access. On
your local machine, keep SSH access and GitHub CLI authentication when cloning a
private repository.

## Install the server dependencies

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-buildx sqlite3 gh
sudo usermod -aG docker $USER

curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
```

Log out and back in after changing Docker group membership, or verify it with
`sg docker -c "docker ps"`.

## Clone and build

```bash
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
git clone git@github.com:crypdick/pynchy.git ~/src/pynchy
cd ~/src/pynchy
uv sync
uv sync --extra whatsapp       # only when using WhatsApp
sg docker -c './src/pynchy/agent/build.sh'
```

## Configure credentials and a channel

Copy the configuration examples, configure the model route, and keep secrets in
`.env`:

```bash
cd ~/src/pynchy
cp config-examples/config.toml.EXAMPLE config.toml
cp config-examples/litellm_config.yaml.EXAMPLE litellm_config.yaml
```

Follow [Channels](../channels/index.md) for Slack, Discord, or TUI setup. To
use WhatsApp, authenticate from the server and scan its terminal QR code:

```bash
uv run pynchy-whatsapp-auth
```

## First run and service

```bash
uv run pynchy
```

The first foreground run creates the initial workspace, installs the user
service, enables it for boot, and enables user lingering. Verify it, stop the
foreground process, then start the service:

```bash
systemctl --user start pynchy
systemctl --user status pynchy
journalctl --user -u pynchy -f
```

Use `config-examples/pynchy.service.EXAMPLE` as the unit-file reference.

## Connect the TUI

The local client prefers Pynchy's Unix socket:

```bash
uv run pynchy --tui
```

For remote access, bootstrap a bearer token and explicitly enable a public
listener. Tailscale alone does not authorize the HTTP API; follow [Control plane
access](../usage/control-plane.md#enable-remote-tui-access).

## Maintain and update the server

Use your normal operating-system update policy. The optional templates in
`config-examples/` install unattended upgrades and a daily Docker cleanup/reboot
timer. Keep those live files under your own operational control.

For remote source updates, enable deployment separately from public TUI access;
see [remote deployment](../usage/control-plane.md#enable-remote-deployment-separately).
Pynchy validates imports and rolls back a failed deployment.
