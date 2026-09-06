---
name: Documentation Manager
description: Use when writing or reviewing pynchy documentation, deciding where to document things, updating the docs, checking doc consistency, or fixing broken links. Covers information architecture, writing philosophy, tree-shaped navigation, doc-code coupling, no hard-coded usernames, extensibility framing for pluggable subsystems, and when to add code comments.
---

# Documentation Manager

Helps decide where to document things and maintain consistency across Pynchy docs.

## Documentation policy

Read [the contributing style guide](../../../docs/contributing/contributing-docs.md)
for audience, placement, style, and information architecture. Keep those rules there;
this skill owns the verification procedure.

## Validation

**Before committing:**
```bash
# Check for broken links
uv run mkdocs build --strict
```

**After moving/renaming files:**
1. Search for all references to old name
2. Update each reference
3. Update `mkdocs.yml` nav
4. Test with `uv run mkdocs build --strict`
