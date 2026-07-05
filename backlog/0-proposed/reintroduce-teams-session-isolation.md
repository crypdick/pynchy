# Reintroduce Teams with per-teammate session isolation

## Problem

Native Teams tools (`TeamCreate`/`TeamDelete`/`SendMessage`) are deliberately **not** allow-listed in either agent core. This closes session-transcript branching by construction: with no way to spawn teammates that write to the leader's session JSONL, a resume cannot pick a stale branch tip. See `.claude/skills/pynchy-dev/references/session-transcript.md`.

The TS original guarded this with a `resumeSessionAt` anchor (last assistant UUID) on every resume. That primitive was lost in the Python port and has no CLI successor, so re-enabling Teams today would reopen the stale-branch failure mode.

## Requirement

Multi-agent Teams collaboration without corrupting the leader's transcript.

## Proposed direction

Give each teammate its own isolated session (separate JSONL / project dir) rather than sharing the leader's, so concurrent CLI subprocesses never contend for the same branch tip. Only after per-teammate isolation exists should the Teams tools be re-added to the core allow-list.

`Task` sidechains already satisfy this (they write off the main chain and resume safely) and remain allowed — they are the interim substitute for delegation.

## Status

Proposed. Guardrail is currently enforced by omission from the tool allow-list; this item tracks the work needed to lift that safely.
