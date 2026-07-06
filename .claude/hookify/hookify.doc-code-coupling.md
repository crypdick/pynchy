---
name: doc-code-coupling
enabled: true
event: file
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.(py|md|toml|yaml|yml)$
action: warn
---

You're editing a file that may couple code to documentation. When a specific value in code is *also* documented in prose (env var allowlists, blocked patterns, mount tables, config keys, user-facing names), the two drift out of sync unless they reference each other.

**The principle:** at the code site, leave a comment pointing to the doc that references this value.

```python
# NOTE: Update docs/architecture/security.md § Credential Handling if you change this list.
allowed_vars = ["PATH", "HOME", "USER"]
```

**When authoring or modifying code:** if you introduce a value that is (or should be) documented elsewhere, add a `NOTE:` comment naming the doc file and section.

**When modifying a value that already has a `NOTE:`:** read the referenced doc and update it in the same change. Don't merge code changes that silently contradict their own documentation.

**When editing docs:** if you find yourself restating a concrete value (a list of patterns, a table of mounts, an allowlist), consider whether the code site deserves a `NOTE:` back-pointer.

This is not a hard rule — for small projects with no published docs, skip it. Apply it where code and prose describe the same thing and drift would mislead readers.
