# Tool-result reduction

## Goal

Reduce context spent on known-noisy tool results without changing the command
that ran or concealing evidence needed for correct agent decisions.

## Scope

- Define a core-neutral post-tool-result extension point that agent cores can
  opt into after a tool completes and before its result enters model context.
- Add an opt-in reducer for safe, recognized inventory-style output such as
  repository status and package listings.
- Preserve raw result, exit status, and reduction metadata in durable trace
  storage, with an agent-accessible way to request the unmodified result.
- Keep exact file reads, errors, and ambiguous command sequences verbatim.
- Test identical command execution, exit codes, raw-result recovery, reduction
  classification, and disabled behavior across every supported core surface.

## Boundary

This does not rewrite shell input, rerun commands, change exit codes, or serve
as a blind truncation layer. It is a cross-core runtime capability before it
can become a separately distributed plugin.
