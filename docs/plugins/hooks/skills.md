# Skill hook

## `pynchy_skill_paths`

Provide agent skill directories that Pynchy filters through the selected
profile's `skills` field before copying them into each core's session directory.

```python
@hookimpl
def pynchy_skill_paths(self) -> list[str]:
    return [str(Path(__file__).parent / "skills" / "code-review")]
```

Return absolute paths to directories containing `SKILL.md` with supported YAML
frontmatter:

```yaml
---
name: code-review
description: Review code for bugs and style issues.
tier: community
---
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No | Defaults to the directory name. |
| `description` | No | Human- and agent-facing summary. |
| `tier` | No | Selection label; defaults to `community`. |
| `allowed-tools` | No | Optional tool permissions. |

`core` skills are always included. `community` and any project-defined tier are
included only when the profile selects them. A profile can select tier names or
individual skills:

```toml
[profiles.my-profile]
skills = ["core", "dev"]
```

When `skills` is unset, Pynchy includes only core skills. `["*"]` includes every
ordinary available skill. It does not bypass companion-skill authorization.

`allowed-tools` and profile skill selection do not grant environment
credentials. A credential-requiring skill must accompany a selected tool whose
TOML declaration owns the requirements. See
[Tool access and secrets](../../usage/tool-access.md).
