# Tool Access and Secrets

Configure tools as the only authority for provider credentials and companion
skills. This keeps a workspace's access reviewable in `pynchy.toml` and limits
each credential to the process that needs it.

## Define Tools

Declare credential names and companion skills on the tool. Store credential
values in the Pynchy host process environment, not in TOML.

```toml
--8<-- "config-examples/tool-access.toml"
```

`required_env` controls availability. Pynchy disables the tool when any named
variable has no value. `optional_env` never controls availability; Pynchy
passes each optional variable that has a value.

`permissions` supplies policy defaults to every workspace that selects the
tool. Profiles and workspaces can add stricter rules. See
[Permissions](security.md#permissions) for matching and precedence.

Keep non-secret runtime constants, such as a bind address, in
`[tools.<name>.mcp].env`. Declare secrets with `required_env` or
`optional_env`. Pynchy does not support `env_forward`.

## Select Tools

Profiles remain the composition mechanism. Selecting a tool also installs its
companion skills. The profile and workspace declarations in the complete
example above compose Linear, GitHub CLI, and Proton Mail access.
Do not add a separate `[bundles]` layer.

Secret-free instructional skills can remain directly selectable in
`profiles.<name>.skills`. A skill that needs provider credentials must belong
to a tool; do not declare environment requirements on a skill. Naming that
companion skill directly in a profile, granting a learned skill, or editing a
`SKILL.md` file cannot grant its credential access. Only the selected TOML tool
can do that. Standalone and learned skills never request or receive environment
variables.

Plugins can contribute tool implementations and companion skill directories
through their existing hooks. TOML still declares the authorization-bearing
tool. Plugin host-service credentials stay in the host service unless a
selected tool explicitly declares and exposes them.

## Choose the Exposure Boundary

| Tool configuration | Credential destination |
|--------------------|------------------------|
| `type = "mcp"` or another runtime-backed tool | Only that tool's Docker, script, or stdio process |
| `type = "workspace"` | The agent workspace; this type has no separate runtime |
| `expose_env_to_workspace = true` | The tool runtime, when present, and the agent workspace |

Runtime-backed tools default `expose_env_to_workspace` to `false`. Use `true`
only when a companion skill calls the provider directly, as the Linear example
does.

Pynchy passes selected names to Docker with value-free `-e NAME` flags and
supplies values through the Docker CLI process environment. Script, stdio, and
direct-host processes receive a small operational environment plus their
explicitly selected variables. Pynchy does not write generated workspace
environment files or mount an environment directory.

## Handle Missing Access

When a required variable has no value, Pynchy removes only the affected tool
and its companion skills. The agent receives a notice that names the tool and
missing variables without including values. It can report the missing
capability or use another already-authorized tool, such as Linear, to record an
issue. It cannot change the running grant.

Status, notices, and agent context expose requirement names and availability
only. They never expose credential values.

Profile composition determines the selected tools. Scheduled agent jobs and
ordinary dynamic child threads resolve the same access as their parent
workspace. A semantic child workspace uses its own selected profiles.

## Materialize Host Secrets

For local development, put declared values in the ignored root `.env`. Pynchy
loads that file into its host process.

For production, copy `config-examples/proton-pass.env.EXAMPLE` to
`data/proton-pass/pynchy.env` and keep only `pass://` references in that
operator-owned template:

```dotenv
--8<-- "config-examples/proton-pass.env.EXAMPLE"
```

Start Pynchy through the repository wrapper:

```bash
scripts/run_pynchy.sh
```

When the template exists, the wrapper starts
`pass-cli run --env-file data/proton-pass/pynchy.env -- uv run pynchy`.
Without the template, it starts Pynchy normally so local `.env` development
continues to work. The template belongs to the host deployment, not a
workspace or skill. Pynchy reads the resulting process environment and never
invokes Proton Pass for an individual agent or writes resolved values into the
workspace.

After changing the tool declarations or host environment, restart through the
normal managed deployment flow. Inspect requirement names and availability,
never credential values.
