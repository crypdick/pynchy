# Headless Linux server

Deploy Pynchy on a headless Linux server as a systemd user service. The examples
use Tailscale and WhatsApp; configure a different channel when WhatsApp does not
fit the deployment.

## Prerequisites

On the server, use Ubuntu or Debian with Docker, a reachable Temporal service,
Proton Pass CLI authenticated for the service account, and
[Tailscale](https://tailscale.com/download) when you need remote access. On your
local machine, keep SSH access and GitHub CLI authentication when cloning a
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
git clone git@github.com:YOUR-ACCOUNT/pynchy-personalization.git \
  data/personalization
uv run pynchy validate-personalization data/personalization
sg docker -c './src/pynchy/agent/build.sh'
```

## Configure credentials and a channel

Configure model routes and non-secret settings in the private
`data/personalization/` checkout. Declare each tool's credential names in
`pynchy.toml`, then materialize gateway, provider, channel, and selected tool
values into the managed Pynchy process through Proton Pass. Keep the Pass
reference template outside workspaces and source control. See
[Tool access and secrets](../usage/tool-access.md#materialize-host-secrets) and
[Personalization repository](../usage/personalization.md).

Start from the reference-only template, replace its sample item paths, and add
the remaining gateway and channel requirements:

```bash
mkdir -p data/proton-pass
cp config-examples/proton-pass.env.EXAMPLE data/proton-pass/pynchy.env
```

Follow [Channels](../channels/index.md) for Slack or Discord setup. To
use WhatsApp, authenticate from the server and scan its terminal QR code:

```bash
uv run pynchy-whatsapp-auth
```

## First run and service

```bash
scripts/run_pynchy.sh
```

The first foreground run creates the initial workspace, installs the user
service, enables it for boot, and enables user lingering. Verify it, stop the
foreground process, then start the service:

```bash
systemctl --user start pynchy
systemctl --user status pynchy
journalctl --user -u pynchy -f
```

Use `config-examples/pynchy.service.EXAMPLE` as the unit-file reference. Replace
its path placeholders before enabling it. The unit starts
`scripts/run_pynchy.sh`, which materializes
`data/proton-pass/pynchy.env` through Proton Pass when that template exists.

## Maintain and update the server

Use your normal operating-system update policy. The optional templates in
`config-examples/` install unattended upgrades and a daily Docker cleanup/reboot
timer. Keep those live files under your own operational control.

For remote source updates, enable deployment separately from public diagnostic access;
see [remote deployment](../usage/control-plane.md#enable-remote-deployment-separately).
Pynchy validates imports and rolls back a failed deployment.
