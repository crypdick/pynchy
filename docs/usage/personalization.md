# Personalization repository

Keep deployment-specific Pynchy configuration in a separate, usually private
Git repository. Check it out at the conventional path:

```text
pynchy/
├── data/
│   ├── defaults/          # public, tracked Pynchy baseline
│   └── personalization/   # independent repository, ignored by Pynchy
└── .env                   # deployment secrets only
```

Pynchy reads the directory. It does not clone, pull, commit, or otherwise
manage the repository.

## Create the repository

Start from the shipped example:

```bash
cp -R config-examples/personalization /tmp/pynchy-personalization
cd /tmp/pynchy-personalization
git init
git add .
git commit -m "Initialize Pynchy personalization"
```

Push that directory to a private remote, then check it out inside each Pynchy
installation:

```bash
git clone git@github.com:YOUR-ACCOUNT/pynchy-personalization.git \
  data/personalization
```

Do not use a Git submodule. `data/personalization/` is ignored by the public
Pynchy repository, so the nested checkout remains independent and Pynchy does
not publish its URL or commit identity.

## Directory contract

```text
data/personalization/
├── pynchy.toml
├── litellm.yaml
├── automations/
│   ├── weekly-review.toml
│   └── weekly-review.md
├── prompts/
│   └── my-workspace-prompt.md
└── skills/
    └── my-skill/
        └── SKILL.md
```

`pynchy.toml` and `litellm.yaml` are required. The other directories are
optional. Pynchy refuses normal service startup when the required files are
missing or the tree does not validate.

Configuration resolves in this order, from lowest to highest priority:

1. `data/defaults/pynchy.toml`
2. `data/personalization/pynchy.toml`
3. `.env` and process environment variables

Nested TOML mappings merge recursively. Lists and scalar values replace the
lower layer. `gateway.litellm_config` is convention-owned: Pynchy wires it to
`data/personalization/litellm.yaml`, so do not set that field yourself.

Keep API keys, tokens, and passwords in the root `.env`, not in the
personalization repository. Environment overrides use `__` between nested
fields, such as `GATEWAY__MASTER_KEY`.

## Automations

Each direct `automations/*.toml` file declares one automation. Its filename
without `.toml` is the stable job ID:

```toml
# data/personalization/automations/weekly-review.toml
schema_version = 1

[job]
schedule = "0 9 * * 1"
workspace = "admin"
prompt_file = "weekly-review.md"
display_name = "Weekly review"
```

Relative `prompt_file` paths resolve from the automation file's directory. A
personalized automation replaces a same-named public default automation as one
unit. Do not also declare the same ID under `[jobs]` in `pynchy.toml`.

Runtime schedule state and run evidence remain in SQLite and Temporal. The
automation file is the desired-state declaration, not an execution log.

## Skills

Pynchy copies skills from these sources in order:

1. Public defaults in `data/defaults/skills/`
2. Personalized skills in `data/personalization/skills/`
3. Code-coupled built-in and plugin skills

A personalized skill can replace a same-named public default. It cannot shadow
a code-coupled skill. Each skill directory must contain a `SKILL.md` whose
frontmatter has matching `name`, non-empty `description`, and non-empty `tier`
fields.

## Prompts

Prompt names select Markdown files by basename. Pynchy reads a personalized
file from `prompts/<name>.md` when present; otherwise it reads the public
baseline in `data/defaults/prompts/`. A personalized file replaces, rather than
extends, a same-named default.

## Validate in CI

Validate a checkout locally:

```bash
uv run pynchy validate-personalization data/personalization
```

The command accepts any repository path, so the personalization repository can
validate itself in CI by checking out the latest Pynchy and running:

```bash
cd ../pynchy
uv sync --locked
uv run pynchy validate-personalization "$GITHUB_WORKSPACE"
```

The example repository includes a GitHub Actions workflow with this contract.
Validation covers layered Pynchy settings, automation documents and prompt
paths, skill metadata, LiteLLM structure, and configured model route names.
