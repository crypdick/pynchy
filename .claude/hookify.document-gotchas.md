---
name: document-gotchas
enabled: true
event: stop
pattern: .*
action: warn
---

**Before finishing: did you discover any gotchas?**

If you encountered non-obvious behavior, subtle bugs, or surprising code interactions during this task, document them **in the appropriate context** — not in the top-level CLAUDE.md:

- **Code comment** near the tricky line/function explaining the "why" (preferred)
- **Docstring** on the function or class if it affects callers
- **Module-level comment** if it's a file-wide concern (e.g. query filter semantics in db.py)
- **Hookify rule** if it's a pattern that could bite someone again

Keep CLAUDE.md for high-level project context only. Gotchas belong next to the code they describe.
