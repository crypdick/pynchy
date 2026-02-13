---
name: ruff-check-require-fix
enabled: true
event: bash
conditions:
  - field: command
    operator: regex_match
    pattern: ruff\s+check
  - field: command
    operator: not_contains
    pattern: --fix
action: block
---

**Always run `ruff check` with `--fix`.**

Running `ruff check` without `--fix` just reports errors you'll need to fix anyway.
Use `ruff check --fix` to auto-fix, then only manually address remaining issues.
