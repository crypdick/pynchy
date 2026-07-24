# Configuration layers

Pynchy separates public product defaults, private deployment intent, secrets,
and runtime state.

```text
data/defaults/                  public Pynchy repository
        │
        ▼ recursive merge
data/personalization/           independent deployment repository
        │
        ▼ field overrides
.env and process environment    deployment secrets and emergency overrides
        │
        ▼ typed validation
Settings                        Pynchy-owned Pydantic schema
```

The Pynchy repository owns `data/defaults/` and the validation logic. The
operator owns the checkout at `data/personalization/`. The path is a convention,
not a Git relationship: the public repository ignores the directory and
Pynchy's runtime never performs repository operations inside it.

## Merge boundary

`pynchy.toml` mappings merge recursively from defaults to personalization.
Scalar and list values replace the lower layer. Pydantic validates the composed
mapping only after the merge, so a higher layer can extend a mapping declared
by a lower layer without copying the entire section.

Automations and skills use identity-aware composition instead of a generic
directory overlay:

- An automation's identity is its TOML filename. The personalized document
  replaces a same-named default document.
- A skill's identity is its directory and frontmatter name. Personalized skills
  can replace public defaults but cannot shadow code-coupled core or plugin
  skills.

This distinction keeps implementation-coupled behavior with its code while
letting generic settings, skills, and automations behave like deployment
infrastructure.

## Desired state and runtime state

The personalization repository declares desired state. It does not store:

- SQLite message, workspace, or task rows
- Temporal schedules, workflow history, or retries
- generated LiteLLM configuration
- container state or worktrees

Those remain under Pynchy's ignored runtime data paths. At startup,
reconciliation applies the typed desired state to SQLite and Temporal.

The user-authored LiteLLM source is
`data/personalization/litellm.yaml`. Pynchy filters and writes the runtime copy
under `data/litellm/`; it never modifies the source file.

## Validation boundary

Normal startup requires a complete personalization tree. The
`validate-personalization` command accepts an arbitrary path so the independent
repository can validate pull requests against the current Pynchy schema without
being nested inside a live checkout.

Environment values are intentionally absent from this static validation pass.
That lets CI validate private desired state without production secrets. Startup
still performs the existing runtime route and credential checks after
environment overrides are applied.
