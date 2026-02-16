# Style Guide

## Documentation Philosophy

Write documentation from the **user's perspective and goal**, not chronological order. The user is trying to achieve something—help them achieve it by disclosing information when it makes sense in their pursuit of that goal.

**Bad (chronological):** "First we added X, then we refactored Y, then we discovered Z needed changing..."

**Good (goal-oriented):** "To accomplish [goal], do [steps]. Note: [context when relevant to the task]."

Structure documentation around:
- What the user is trying to do
- What they need to know to do it
- Relevant context disclosed at the point of need
- Not the history of how the code evolved

## Information Architecture

Documentation follows a **tree structure** optimized for selective reading by both humans and agents.

### Single source of truth

Every concept is explained in exactly one place. If the same topic appears in multiple files, consolidate it into one canonical location and cross-link from everywhere else. Duplication drifts out of sync and wastes context.

### Tree-shaped navigation

- **Near the root** (e.g., `CLAUDE.md`, top-level READMEs): mostly links and short summaries that point deeper into the tree.
- **Folders** group related docs into categories.
- **Leaf nodes** are where the actual information lives — detailed explanations, examples, and reference material.

This lets agents navigate the tree and selectively read only what's relevant, instead of loading everything at once.

### Small, focused files

Each file covers **one topic**. If a page grows to cover multiple concerns, split it. Agents should never blow up their context reading a single file.

- Prefer cross-linking over repeating information.
- A file that requires scrolling through unrelated sections to find what you need is too big or too broad.

## Doc-Code Coupling

When a specific value in code is also documented (env var allowlists, blocked patterns,
mount tables, user names, etc.), add a comment at the code site:

    # NOTE: Update docs/architecture/security.md § Credential Handling if you change this list
    allowed_vars = [...]

This keeps docs in sync without requiring developers to memorize which docs reference which code.
The comment should reference the specific doc file and section.

## Code Comments: Capture User Reasoning

When the user gives an instruction or makes a design decision **and explains their reasoning**, capture that reasoning as a comment in the code — right where the decision is implemented. Future maintainers should be able to understand the intent without leaving the code context.

- Only add comments when the user provides a *reason*, not for every instruction
- Place the comment at the point of implementation, not in a separate doc
- Preserve the user's reasoning faithfully — don't paraphrase away the nuance
