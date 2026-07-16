# Pynchy Matrix reader

A deliberately read-only Matrix client for the Pynchy agent account. It uses the maintained Matrix Rust SDK with a persistent encrypted SQLite crypto store so it can decrypt messages in rooms the account has explicitly joined.

It has no commands for sending messages, creating or joining rooms, reacting, editing, or changing room state. `messages` and `tail` both require an explicit room ID.

The first login reads the Matrix password only from standard input. The session and store-encryption key are created with mode `0600`; the SQLite store directory is mode `0700`.

Verify the `Pynchy Matrix reader` device from Element before trusting it with encrypted conversations. A fresh device cannot decrypt historical messages; it receives room keys for new messages after verification.

Typical agent-facing use:

```sh
pynchy-matrix-reader rooms
pynchy-matrix-reader messages --room '<explicit room ID>' --limit 50
pynchy-matrix-reader tail --room '<explicit room ID>'
```
