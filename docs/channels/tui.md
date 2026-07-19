# Terminal UI

Use the built-in terminal UI to talk to Pynchy locally without an external
messaging service.

```bash
uv run pynchy --tui
```

The TUI uses the local Unix socket and falls back to loopback HTTP when needed.
It needs no extra dependency or configuration. Remote connections require an
explicit public bind and bearer token; see [Control plane access](../usage/control-plane.md#enable-remote-tui-access).
