# Backlog

Start at [TODO.md](TODO.md) — it's the index for all work items.

## Pipeline

```
0-proposed/       Agent-generated ideas awaiting human review.
1-approved/  Approved ideas. No plan yet.
2-planning/       Draft plan written. Awaiting human sign-off.
3-ready/          Plan approved. Ready for an agent to pick up.
4-in-progress/    Being implemented.
5-completed/        Done.
denied/           Rejected (kept for context).
```

## Flow

```
Agent has idea  → 0-proposed/     → human approves → 1-approved/
Human has idea  → 1-approved/  (skips 0)

Ready to plan   → 2-planning/     → human approves → 3-ready/
Agent picks up  → 4-in-progress/  → done           → 5-completed/

Rejected at any gate → denied/
```

## Plan File Format

```markdown
# Item Title

Brief description of what and why.

## Context
Background, links, relevant code paths.

## Plan
Implementation steps (filled in during planning phase).

## Done
What was actually implemented (filled in on completion).
```
