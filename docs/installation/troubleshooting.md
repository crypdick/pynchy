# Installation troubleshooting

Use these checks when Pynchy cannot start, authenticate, build a container, or
accept a remote connection.

## Missing provider credentials

Set the provider credential in `.env` and reference it from
`litellm_config.yaml`, or configure the built-in gateway secret. Then restart
the Pynchy service.

## WhatsApp QR code does not scan

- Confirm that the phone and host can reach each other, or use SSH.
- Scan the Unicode QR code directly in the terminal.
- Try another terminal emulator if it does not render correctly.

Linked devices can expire after roughly 30 days without activity. Re-run
`uv run pynchy-whatsapp-auth`, then restart Pynchy.

## Container build fails

On macOS, start Apple Container or Docker and update the selected runtime. On
Linux, start Docker and confirm the user belongs to the `docker` group.

## Pynchy does not accept a remote connection

Pynchy binds to `127.0.0.1:8484` by default and intentionally rejects remote
clients. Confirm Tailscale connectivity when you use it, then configure a
public listener and bearer token through [Control plane access](../usage/control-plane.md#enable-remote-tui-access).

## macOS LaunchAgent exits immediately

Validate the installed plist and confirm launchd received resolved absolute
paths rather than literal `$HOME` strings. Inspect `logs/pynchy.error.log` and
`logs/pynchy.general.log` for the application context.

## Linux service does not start after reboot

Check user lingering with `loginctl show-user $USER | grep Linger`. Enable it
with `sudo loginctl enable-linger $USER` when needed.

## First run does not create the systemd service

Run `uv run pynchy` without `--tui` for the initial setup. The TUI-only command
does not create the service.
