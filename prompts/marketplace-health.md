# Marketplace health

For pending marketplace decision counts, awaiting-reply counts, or Proton
reader health, call `mcp__pynchy__marketplace_health_snapshot` first and treat
its aggregate-only response as authoritative.

Do not use Bash, inspect `/Users/...` paths, invoke `pm-cli`, or search workspace
files for these values. Those host paths are intentionally unavailable inside
the agent container. If the native tool returns an error, report that bounded
error instead of probing marketplace or mailbox state through another path.
