# Tunnels

Pynchy's tunnel connectivity detection system. Use this page to make sure an
explicitly enabled remote control-plane listener can traverse your selected tunnel.

Tunnels are pluggable. The built-in plugin detects Tailscale, but alternative providers (Cloudflare Tunnel, WireGuard, etc.) can be added via plugins.

## What Tunnels Do

Pynchy binds its HTTP control plane to loopback by default. Remote TUI or deployment
access requires application-layer bearer authentication and an explicit public-bind
option before a tunnel can reach port 8484. See
[Control Plane Access](../usage/control-plane.md).

The tunnel subsystem **detects** whether a tunnel is available — it doesn't create or manage tunnels itself. At startup, Pynchy checks every registered tunnel provider and warns if none are connected.

**This is purely informational.** If no tunnel is detected, Pynchy continues running
normally. Tunnel detection never relaxes the control-plane bind, authentication,
rate-limit, or deployment policy.

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
- Failures fall through to "not connected" — CLI not found, timeouts, and parse errors are all treated the same

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
