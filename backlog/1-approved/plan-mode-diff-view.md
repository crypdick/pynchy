# Plan Mode Diff View

**Status**: Approved (user request)
**Date**: 2026-02-16

## Problem

When the agent exits plan mode (`ExitPlanMode` tool call), the user sees a truncated tool-call notification but never sees the actual plan content. The plan file is written/edited inside the container during plan mode, but the tool_result content was always broadcast as the generic "📋 tool result" placeholder.

## V1 — Broadcast full tool_result for plan mode tools (DONE)

Simple fix: when a `tool_result` follows an `ExitPlanMode` or `EnterPlanMode` tool call, broadcast the full result content instead of the generic placeholder.

Implementation: `output_handler.py` tracks the preceding `tool_name` per chat and checks it against a `_VERBOSE_RESULT_TOOLS` allowlist when formatting tool_result broadcasts.

## Future — Diff view

A richer version could show a diff of the plan file (before vs after plan mode edits). This would require tracking file state across tool calls, which is more complex. Possible approaches:

1. **Agent-runner hook**: Detect `ExitPlanMode` in the Claude core, read the plan file, compute diff against a cached baseline
2. **Track Write/Edit calls**: During plan mode, accumulate edits targeting the plan file to reconstruct the diff
