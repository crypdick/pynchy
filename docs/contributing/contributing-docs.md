# Style Guide

## Write docs for users, not developers

Users want to get something done. Docs should help them do it.

Structure docs around tasks. Docs are not for:

- chronicling the evolution of the codebase
- internal implementation details users don't need (those go in Architecture)
- encyclopedic coverage of every feature and concept

Each page should open by saying what it's about and why the reader would want to read it. For example: "An overview of the core constructs and concepts of <feature>. Understand these to navigate, configure, and use Pynchy."

Organize content so information appears in the most relevant context, at the point of need.

## Information Architecture

Documentation follows a **tree structure** optimized for selective reading by both humans and agents.

### Single source of truth

Every concept is explained in exactly one place. If a topic appears in multiple files, pick one canonical location and cross-link from the rest. Duplicates drift out of sync and waste context.

### Tree-shaped navigation

- **Near the root** (e.g., `CLAUDE.md`, top-level READMEs): links and short summaries that point deeper into the tree.
- **Folders** group related docs into categories.
- **Leaf nodes** hold the actual content — detailed explanations, examples, reference material.

Agents can then walk the tree and read only what's relevant, instead of loading everything.

### Small, focused files

Each file covers **one topic**. If a page covers multiple concerns, split it. Agents should never blow up their context reading a single file.

- Cross-link instead of repeating.
- If you have to scroll past unrelated sections to find what you need, the file is too big or too broad.

## Usage Guide vs Architecture Guide

User-facing features belong in the **usage guide** (`docs/usage/`), not architecture. If a user needs to know about a subsystem to *use* Pynchy — how memory works, how scheduled tasks behave, what container mounts are available — document it in usage.

The **architecture guide** (`docs/architecture/`) is for internal design details that help developers and plugin authors understand *how* things work under the hood — message routing internals, IPC protocol, security boundaries.

Rule of thumb: writing for a Pynchy *user* goes in usage. Writing for someone building, debugging, or extending Pynchy goes in architecture.

## Document for a Pluggable System

Everything in Pynchy is a plugin. Docs should reflect that — no subsystem page should read as "this is how it works, period." Frame each subsystem as "this is the **built-in** approach" and make it clear that alternatives can be swapped in via plugins.

### Structure pages for extensibility

- **Lead with the concept**, not the implementation. Explain *what* the subsystem does before *how* the built-in plugin does it.
- **Use headings like "Built-in: \<plugin name\>"** for the default implementation, so it reads as one option rather than the only option.
- **Keep plugin-specific details in their own section** so an alternative can be documented alongside without restructuring the page.
- When listing capabilities, separate what the *subsystem* guarantees (the hookspec contract) from what the *built-in plugin* provides.

### Subsystem CTA

Every page documenting a pluggable subsystem should end with a short call-to-action inviting users to customize it:

```markdown
---

**Want to customize this?** Write your own plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
```

Adjust the relative link depth as needed. Keep the CTA brief — two sentences max.

## Doc-Code Coupling

Follow [the design convention](https://github.com/crypdick/pynchy/blob/main/CONVENTIONS.md#keep-code-and-its-documentation-coupled)
when code and prose describe the same value. Keep the rule in `CONVENTIONS.md`.

## No Hard-Coded Usernames

Never hard-code a user's name, username, email, or home directory path into documentation. Use generic placeholders instead (e.g., `<user>`, `~`, `your-key-here`). This keeps docs portable and avoids leaking personal info into the repo.

## Code Comments: Capture User Reasoning

When the user gives an instruction or makes a design decision **and explains their reasoning**, capture that reasoning as a comment in the code — at the point where the decision is implemented. Future maintainers should be able to understand the intent without leaving the code context.

- Only add comments when the user provides a *reason*, not for every instruction.
- Put the comment at the point of implementation, not in a separate doc.
- Preserve the user's reasoning faithfully — don't paraphrase away the nuance.
