# Comparison to ClawHub self-improvement skills

## Conclusion

Pynchy already has a stronger host-native improvement loop than the reviewed
prompt-only skills: bounded turn packets carry provenance and recovered-error
snippets; a hidden Temporal reviewer can create durable vault notes or learned
skills; selected skills activate in both Claude and Codex homes; and the code
improver works in an isolated scheduled workspace that can merge and push.

Do not install a ClawHub skill, OpenClaw heartbeat, external evolution proxy,
or a second memory hierarchy. Adapt the narrow governance patterns that close
Pynchy's current auditability gaps: a candidate ledger, recurrence evidence,
explicit promotion states, immutable approved skill snapshots, and host-side
merge evidence.

## Scope and evidence

The comparison reviewed Pynchy's learning packet, reviewer, learned-skill
activation, scheduled code-improver, and worktree merge paths, plus the
Evolver, Self-Improving, and self-improving-agent ClawHub skills. Focused
learning-path, packet, reviewer, pipeline, mirror, and activation coverage
passed 71 tests during the audit.

The ClawHub sources provide ideas rather than dependencies. Evolver relies on
a Node process, EvoMap Hub proxy/mailbox, remote task subscription, and asset
exchange; its page also reported conflicting GPL-3.0-or-later and MIT-0
license signals. Neither makes it an acceptable Pynchy runtime dependency.

## Existing Pynchy strengths

- A successful turn produces a bounded review packet with user messages,
  final answer, tool counts, recovered-error snippets, and provenance.
- A hidden Temporal reviewer can produce durable notes or reusable skills in
  the vault.
- Validated learned skills activate selectively for both supported agent-home
  families.
- The code improver works in an isolated scheduled workspace and follows the
  managed worktree publication path.

## Improvements to adapt

### Create a typed learning-candidate ledger

Record corrections, recovered errors, failed turns, feature requests, and
workflow improvements as sanitized, provenance-bearing candidates rather than
free-form notes or raw transcripts. Use a stable pattern key to deduplicate;
update occurrence count and first/last-seen timestamps instead of creating
duplicates.

Candidates need an append-only event history and states such as pending,
resolved, rejected, and promoted. Associate code-affecting outcomes with the
commits or pull requests that resolved them. A periodic Temporal triage flow
can report pending and stale candidates without creating another scheduler.

### Require evidence before behaviour changes

Do not learn from silence. Treat explicit corrections as candidate evidence and
require repeated successful application before a reviewer proposes a
behaviour-changing prompt or learned skill. A suitable initial threshold is
three observations across two turns within 30 days. Workspace-owner approval
must precede activation; explicitly requested durable facts can still be filed
immediately.

Current automatic review starts only after a clean success, so unrecovered
failures remain log-only. Capture those failures deterministically in the
candidate ledger rather than assuming success-only review represents the
system's learning surface.

### Make learned-skill promotion immutable and fail closed

The approved [Cop-gated learned skill vetting](../1-approved/cop-skill-vetting.md)
plan owns the detailed skill-publication boundary. It must keep imported and
reviewer-produced bundles in quarantine, perform deterministic structural
checks plus a dedicated Cop bundle inspection, fail closed if that inspection
cannot produce a valid verdict, and promote only an immutable manifest-backed
snapshot. Persistent grants must pin a reviewed digest, while broad selectors
must resolve promoted snapshots only.

Skill text can declare intent and inform review; it never grants filesystem,
network, browser-auth, process, package-install, secret, or host-admin powers.
Workspace and tool policy remain authoritative. Host Python plugins stay out of
scope because they execute at discovery and need an explicit trusted-plugin
installation boundary.

### Require host-side validation evidence before code-improver publication

The code-improver prompt can request testing, but a successful generic session
merge must not be accepted merely because the agent says it validated work.
The host-side publication policy should require declared validation evidence
appropriate to the change before merge and push, with exceptions recorded as
operator decisions.

## Explicit rejections

- No EvoMap or other remote evolution trust path.
- No autonomous external task ingestion.
- No parallel home-directory memory hierarchy beside the vault.
- No self-modifying shared skills during ordinary conversation.
- No popularity, marketplace audit, or Cop verdict treated as proof of safety.

## Documentation correction

Learning documentation still describes a superseded `data/ipc/learning`
filesystem queue and local worker. Current code routes hidden review through
Temporal, and lifecycle tests assert that no local learning worker starts.
Correct this before using the documentation as operational evidence.

## Recommended sequence

1. Correct the stale learning-path documentation.
2. Implement immutable, Cop-gated learned-skill promotion.
3. Add a typed candidate ledger and deterministic failed-turn capture.
4. Add recurrence thresholds, workspace approval, and Temporal triage.
5. Require host-verified validation evidence before code-improver merge/push.
