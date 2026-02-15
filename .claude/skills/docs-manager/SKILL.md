---
name: Documentation Manager
description: This skill should be used when the user asks to "update the documentation", "where should I document this", "add this to the docs", "document this feature", "check doc consistency", or "fix broken links in docs".
version: 0.1.0
---

# Documentation Manager

Helps decide where to document things and maintain consistency across Pynchy docs.

## Core Principle

Write from the **user's goal**, not chronologically. Bad: "First we added X, then switched to Y..." Good: "To accomplish [goal], do [steps]."

## Where to Document What

Quick decision tree:

**New feature?**
- User-facing behavior → `docs/SPEC.md` (under relevant section)
- Installation requirement → `docs/INSTALL.md`
- Security implication → `docs/SECURITY.md`
- Development workflow change → `.claude/development.md`

**Bug fix?**
- If it revealed architecture issue → `docs/SPEC.md`
- If it needs install change → `docs/INSTALL.md`
- Usually: No doc update needed

**Refactoring?**
- If user-visible → Update relevant docs
- If internal only → No doc update

## File Purposes

| File | What Goes There |
|------|-----------------|
| `README.md` | Philosophy, quick start, high-level overview |
| `docs/INSTALL.md` | Complete installation guide |
| `docs/SPEC.md` | Architecture decisions, technical details |
| `docs/SECURITY.md` | Security model, threat analysis |
| `.claude/*.md` | Development context for Claude Code |

## Validation

**Before committing:**
```bash
# Check for broken links
grep -r "old-filename.md" docs/ README.md CLAUDE.md .claude/

# If using MkDocs
mkdocs build --strict
```

**After moving/renaming files:**
1. Search for all references to old name
2. Update each reference
3. Test with `mkdocs build --strict` if available

## Common Mistakes

❌ Duplicating content across files (link instead)
❌ Chronological explanations ("First we tried X...")
❌ Mixing audiences (keep README brief, details in docs/)
❌ Forgetting to update references after renames

## Link Checking

Link validation runs automatically in pre-commit hooks. If docs have broken links, the commit will fail.

To manually check: `uv run mkdocs build --strict`

Read `.claude/style-guide.md` for the philosophy.
