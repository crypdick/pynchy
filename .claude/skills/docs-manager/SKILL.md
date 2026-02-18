---
name: Documentation Manager
description: This skill should be used when the user asks to "update the documentation", "where should I document this", "add this to the docs", "document this feature", "check doc consistency", or "fix broken links in docs".
version: 0.1.0
---

# Documentation Manager

Helps decide where to document things and maintain consistency across Pynchy docs.

## Style Guide

- Write for a **user trying to achieve a goal**. Don't chronicle the evolution of the codebase. Don't go into unnecessary technical details that are not relevant to the user's goal.
- Follow the [Google Style Guide](https://developers.google.com/style). Write in the present tense. Don't use "we" or "they". Write in the active voice. Don't use "is" or "are".
- Read `docs/contributing/contributing-docs.md` for the full documentation philosophy and information architecture rules.

## Where to Document What

Quick decision tree:

**New feature?**
- Architecture decision → `docs/architecture/` (find the relevant topic file, or create a new one)
- Installation requirement → `docs/install.md`
- Security implication → `docs/architecture/security.md`
- Development workflow change → `.claude/skills/development/SKILL.md`

**Bug fix?**
- If it needs install change → `docs/install.md`
- Usually: No doc update needed

**Refactoring?**
- If user-visible → Update relevant docs
- If internal only → No doc update

## File Purposes

| File | What Goes There |
|------|-----------------|
| `README.md` | Philosophy, quick start, high-level overview |
| `docs/install.md` | Complete installation guide |
| `docs/architecture/security.md` | Security model, threat analysis |
| `docs/architecture/index.md` | Architecture overview and links to topic pages |
| `docs/architecture/*.md` | One topic per file (containers, routing, tasks, etc.) |
| `.claude/skills/` | Development context skills for Claude Code agents |

## Information Architecture Rules

1. **Single source of truth** — Every concept explained in exactly one place. Cross-link, don't duplicate.
2. **Tree-shaped navigation** — Root files have links + short summaries. Leaf files have the actual content.
3. **Small, focused files** — One topic per file. If it covers multiple concerns, split it.

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

## Common Mistakes

- Duplicating content across files (link instead)
- Chronological explanations ("First we tried X...")
- Mixing audiences (keep README brief, details in docs/)
- Forgetting to update references after renames
- Writing mega-files that cover multiple topics

## Link Checking

Link validation runs automatically in pre-commit hooks. If docs have broken links, the commit will fail.

To manually check: `uv run mkdocs build --strict`
