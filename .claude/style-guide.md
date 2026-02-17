# Style Guide

## Write docs for users, not developers

Users are trying to use the system to achieve a goal. The docs should help them achieve their goals.

Structure documentation around how to accomplish tasks using Pynchy. The docs are not meant to:

- chronicle the evolution of the codebase
- go into unnecessary technical implementation details that users don't need to know about (save these in the Architecture section)
- be an encyclopedia of all the features and concepts in the system.

Each doc should start off explaining what the page is about and why the user would want to read it. For example, "This page provides an overview of the core constructs and concepts of <feature>. Understanding these concepts is important for navigating, configuring, and using Pynchy."

The content should be organized such that information is presented in the most relevant context, at the point of need.

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

## Usage Guide vs Architecture Guide

User-facing features belong in the **usage guide** (`docs/usage/`), not the architecture guide. If a user needs to know about a subsystem to *use* Pynchy — how memory works, how scheduled tasks behave, what container mounts are available — document it in the usage guide.

The **architecture guide** (`docs/architecture/`) is for internal design details that help developers and plugin authors understand *how* things work under the hood — message routing internals, IPC protocol, security boundaries.

Rule of thumb: if the reader is a Pynchy *user*, it goes in usage. If the reader is building or debugging Pynchy internals or writing a plugin, it goes in architecture.

## Document for a Pluggable System

Everything in Pynchy is a plugin. Documentation should reflect this — no subsystem page should read as "this is how it works, period." Instead, frame each subsystem as "this is the **built-in** approach" and make it clear that alternatives can be swapped in via plugins.

### Structure pages for extensibility

- **Lead with the concept**, not the implementation. Explain *what* the subsystem does before describing *how* the built-in plugin does it.
- **Use headings like "Built-in: \<plugin name\>"** when describing the default implementation, so it's visually clear this is one option, not the only option.
- **Keep plugin-specific details in their own section** so a future alternative can be documented alongside without restructuring the page.
- When listing capabilities, distinguish between what the *subsystem* guarantees (the hookspec contract) and what the *built-in plugin* provides.

### Subsystem CTA

Every page that documents a pluggable subsystem should end with a short call-to-action inviting users to customize it:

```markdown
---

**Want to customize this?** Write your own plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
```

Adjust the relative link depth as needed. Keep the CTA brief — two sentences max.

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
