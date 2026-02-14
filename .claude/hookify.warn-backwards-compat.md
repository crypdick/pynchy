---
name: warn-backwards-compat
enabled: true
event: file
conditions:
  - field: new_text
    operator: regex_match
    pattern: (?i)(backwards?\s*compat|legacy.{0,20}(shim|compat|support|wrapper|fallback)|deprecated.{0,20}(alias|wrapper|shim|compat)|for\s+(backwards?\s+)?compatibility|re-?export.{0,20}(legacy|compat|old)|_old_\w+\s*=|# ?(removed|legacy|compat))
---

**Backwards-compatibility code detected.**

This project avoids backwards-compat shims, re-exports, legacy wrappers, and compatibility hacks unless absolutely necessary. They bloat the repo and add maintenance burden.

Before writing this code, consider:
- **Is anything actually consuming the old interface?** If not, just delete it.
- **Can callers be updated directly?** Prefer changing call sites over adding shims.
- **Is this a public API with external consumers?** Only then might compat code be justified.

If you're certain this backwards-compat code is necessary, explain why in a comment. Otherwise, remove the old code and update callers directly.

See CLAUDE.md: "Avoid backwards-compatibility hacks like renaming unused _vars, re-exporting types, adding // removed comments for removed code, etc."
