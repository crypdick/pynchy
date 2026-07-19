# Runtime and tunnel hooks

## `pynchy_container_runtime`

Provide a host container runtime such as Apple Container:

```python
@hookimpl
def pynchy_container_runtime(self) -> ContainerRuntime | None:
    return AppleContainerRuntime()
```

Pynchy collects runtimes and selects one through `[container].runtime` or
platform-aware detection. A runtime provides `name`, `cli`, `is_available()`,
`ensure_running()`, and `list_running_containers(prefix)`.

## `pynchy_tunnel`

Provide remote-connectivity detection for Tailscale, Cloudflare Tunnel,
WireGuard, or another tunnel:

```python
@hookimpl
def pynchy_tunnel(self) -> TunnelProvider | None:
    return MyTunnelProvider()
```

Pynchy checks every provider at startup and warns when none connects. A provider
implements `name`, `is_available()`, `is_connected()`, and `status_summary()`.
The built-in Tailscale plugin runs `tailscale status --json` and checks
`BackendState`.
