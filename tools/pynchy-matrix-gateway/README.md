# Pynchy Matrix gateway

The Matrix gateway is a host-only client for the Matrix account that owns Pynchy's bridged chats. It keeps its encrypted store, store key, and Matrix access token in `~/.local/share/pynchy/matrix-gateway/`, all with private filesystem permissions.

It is intentionally separate from `pynchy-matrix-reader`: the reader remains an agent-account, read-only client. The gateway can send only when its host-side caller explicitly provides the body on standard input. Pynchy's MCP trust policy marks that operation as an external, approval-required write.

Build it with:

```sh
cargo build --release --manifest-path tools/pynchy-matrix-gateway/Cargo.toml
```

Log in as the Matrix bridge owner from the host, never from an agent container:

```sh
printf '%s' "$MATRIX_PASSWORD" | ./tools/pynchy-matrix-gateway/target/release/pynchy-matrix-gateway \
  login --homeserver https://matrix.example.com --user @owner:matrix.example.com --password-stdin
```

Then point the host-side Pynchy integration at the binary with `PYNCHY_MATRIX_GATEWAY`. The binary's `send` command is not intended for direct use; external sends must go through Pynchy so its approval gate records and checks them.
