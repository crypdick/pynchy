---
name: taste-enforcer
enabled: true
event: prompt
pattern: don.?t use|always prefer|avoid|never do|instead of|I hate when|stop using|should always|should never|prefer .+ over|ban |forbid
action: warn
---

Keyword hit. The user might have expressed a coding preference or taste.

Reminder: always do the following when the user expresses a code preference that should be enforced going forward. Determine whether it can be codified as:

1. **A pre-commit hook script** — if it's about code patterns that can be detected statically (e.g., "don't use bare except", "avoid print statements"). Create or update a script in `scripts/pre_commit_hooks/` and wire it into `.pre-commit-config.yaml`.

2. **A hookify rule** — if it's about Claude's behavior during sessions (e.g., "don't create utils.py files", "always use NewType for IDs"). Create a `.claude/hookify.{name}.md` rule.

3. **A pyproject.toml setting** — if it maps to an existing tool's configuration (e.g., "ban star imports" → ruff rule).

If the preference is already enforced by an existing hook or rule but the user still had to say something about it, that means the existing enforcement failed to do its job. Identify why it didn't catch the issue (pattern too narrow? wrong event type? missing edge case?) and propose a fix to strengthen the existing hook or rule.

Also: if the user previously expressed a taste in this conversation that this hook missed as a false negative, write a hook for that too.
