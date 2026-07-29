# Marketplace health projection

Give a marketplace workspace read-only access to aggregate decision counts and
mail-reader health. The host returns no buyer, listing, mailbox, or message
content, and the projection never changes marketplace state or mail read flags.

## Configure the projection

Configure the host-owned action ledger and add the built-in tool to the profile
that needs the counts:

```toml
[plugins.marketplace-health.options]
pending_actions_file = "/path/to/marketplace-state/pending_actions.json"
reader_tool = "proton-mail"

[tools.marketplace-health]
type = "builtin"
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false

[profiles.marketplace-poller]
tools = ["marketplace-health"]
```

Select a marketplace executor from the workspace's named pipeline. See
[Prompts and pipelines](../usage/prompts.md).

The action ledger must contain a top-level `pending` object. The projection
counts entries whose `status` equals `pending` or `awaiting_reply`; a missing
status counts as `pending` for compatibility with existing ledgers. Other
statuses remain terminal and do not appear in either count.

The optional `reader_tool` names a configured Proton Mail MCP tool. The health
probe authenticates and performs IMAP mailbox discovery, then discards the
mailbox identifiers. It returns only `ready` or a bounded unavailable reason.
It does not list, fetch, or flag messages.

## Use the projection

The selected marketplace executor should direct the agent to call
`marketplace_health_snapshot` instead of trying host-only paths from inside its
container. Keep that executor selected anywhere the tool is enabled so learned
skills with host-local commands cannot bypass the projection.

The tool returns only this shape:

```json
{
  "counts": {"pending": 0, "awaiting_reply": 0},
  "reader_health": {"status": "unavailable", "reason": "reader_credentials_unavailable"}
}
```

Configure the tool trust fields as shown only when the state file remains
host-owned and the projection code remains aggregate-only. The action runs in
the Pynchy host process, so the workspace does not receive the state path,
Bridge credential, or raw provider failure.
