# Tunnels

This page explains Pynchy's tunnel connectivity detection system. Understanding this helps you ensure your Pynchy instance is reachable remotely — for features like the TUI client, deploy webhooks, and any other HTTP-based access.

Tunnels are pluggable. The built-in plugin detects Tailscale, but alternative providers (Cloudflare Tunnel, WireGuard, etc.) can be added via plugins.

## What Tunnels Do

Pynchy exposes an HTTP server (default port 8484) for the TUI client, deploy webhooks, and SSE event streaming. If you're running Pynchy on a headless server, you need a way to reach that port from other machines.

The tunnel subsystem **detects** whether a tunnel is available — it doesn't create or manage tunnels itself. At startup, Pynchy checks all registered tunnel providers and warns you if none are connected.

**This is purely informational.** If no tunnel is detected, Pynchy continues running normally. You might still have connectivity through other means (direct network access, port forwarding, etc.).

## Startup Check

On boot, Pynchy:

1. Discovers tunnel providers via plugins
2. Checks each provider's availability (is the software installed?)
3. Checks connectivity (is the tunnel actually connected?)
4. Logs the result — `INFO` if connected, `WARNING` if not

If no tunnels are connected at all, you'll see:

```
WARNING: No tunnels connected — remote access may be unavailable.
```

## Built-in: Tailscale

Detects Tailscale connectivity by running `tailscale status --json` and checking the `BackendState` field.

- **Available** when the `tailscale` CLI is installed
- **Connected** when `BackendState == "Running"`
- Handles failures gracefully — CLI not found, timeouts, and parse errors all result in "not connected"

## Tunnel Provider Contract

Plugins implement the `pynchy_tunnel` hook and return an object with:

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Tunnel identifier (e.g., `"tailscale"`, `"cloudflare"`) |
| `is_available()` | `() → bool` | Whether the tunnel software is installed on the host |
| `is_connected()` | `() → bool` | Whether the tunnel is currently connected |
| `status_summary()` | `() → str` | Human-readable status string for logging |

---

**Want to customize this?** Write your own tunnel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
