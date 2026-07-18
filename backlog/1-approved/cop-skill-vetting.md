# Cop-gated learned skill vetting

## Goal

Prevent an unreviewed external or learning-reviewer-produced skill from becoming active in Pynchy. Reuse the Cop as the semantic inspector, while retaining deterministic structural checks and Pynchy's existing tool-policy boundary.

## Problem

Pynchy currently validates learned skills only for safe filesystem shape: an in-root directory, a real `SKILL.md`, no symlinks, and a byte budget. A selected `learned` or `*` tier can then copy that mutable skill into an agent home. Persistent grants name a skill but do not identify the reviewed contents.

The existing Cop inspects inbound content, Bash commands, and host-mutating operations. It does not inspect a complete skill bundle or govern learned-skill publication. Its normal fail-open behavior protects availability for command gates, but must not admit a new persistent instruction bundle.

## Scope

- Add a dedicated Cop `inspect_skill_bundle` context that reviews all materialized skill files and returns a concise, auditable security verdict.
- Write external and reviewer-produced skills to a quarantine/submission path, separate from the active learned-skill registry.
- Preserve deterministic validation before Cop inspection: safe path containment, regular files, no symlinks, bounded size, and stable bundle hashes.
- Fail closed for publication when the Cop or gateway is unavailable, malformed, or flags the bundle. Keep the submission available for later human review; do not activate it.
- Promote a clean, approved bundle as an immutable snapshot with a manifest containing source provenance, per-file hashes, declared capabilities, Cop verdict, and review timestamp.
- Make broad `learned` and `*` selectors resolve only promoted snapshots. Update persistent grants to pin a reviewed snapshot digest so a changed bundle needs fresh vetting.
- Keep capability enforcement below the skill text. Declarations inform review and UI; workspace and tool policy remain authoritative for filesystem, network, browser-auth, packages, secrets, and host actions.

## Non-goals

- Treating a Cop pass, marketplace audit, popularity signal, or YAML declaration as proof that code or instructions are safe.
- Reusing the generic outbound-operation Cop prompt; skill bundles need instruction- and capability-specific review criteria.
- Applying this workflow to host Python plugins. Plugins execute during discovery and require the separate trusted-plugin installation boundary.

## Acceptance criteria

- A reviewer-created or imported skill cannot reach the active registry without structural validation and a successful dedicated Cop review.
- Cop outages and malformed verdicts leave the skill quarantined rather than active.
- Flagged bundles produce an inspectable report and cannot be selected, including by `learned` or `*`.
- Promotion records a content hash; changing any materialized file invalidates a persistent grant until it is vetted again.
- Tests cover clean, flagged, malformed, unavailable, symlink, over-budget, changed-snapshot, and broad-selector cases.

## Related code

- `src/pynchy/host/container_manager/security/cop.py`
- `src/pynchy/host/learning/reviewer.py`
- `src/pynchy/host/learning/skills.py`
- `src/pynchy/host/container_manager/session_prep.py`
