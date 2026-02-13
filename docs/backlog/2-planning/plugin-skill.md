# Skill Plugins

## Overview

Enable agent skills (instructions/capabilities) to be provided by external plugins. Skills define what the agent can do and how to do it.

## Dependencies

- Plugin discovery system (plugin-discovery.md)

## Design

### SkillPlugin Class

```python
class SkillPlugin(PluginBase):
    """Base class for skill plugins."""

    categories = ["skill"]  # Fixed

    @abstractmethod
    def skill_paths(self) -> list[Path]:
        """Return paths to skill directories.

        Each directory should contain:
        - SKILL.md (skill definition)
        - Optional supporting files

        The directory structure is copied to the agent's session dir.
        """
        ...
```

## Example: Calendar Skill Plugin

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-calendar-skill"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
calendar-skill = "pynchy_plugin_calendar_skill:CalendarSkillPlugin"
```

**Plugin structure:**
```
pynchy-plugin-calendar-skill/
├── pyproject.toml
└── src/
    └── pynchy_plugin_calendar_skill/
        ├── __init__.py          # exports CalendarSkillPlugin
        ├── plugin.py            # SkillPlugin implementation
        └── skills/
            └── calendar/
                ├── SKILL.md     # Calendar management skill
                └── examples.md  # Optional: usage examples
```

**plugin.py:**
```python
from pathlib import Path
from pynchy.plugin import SkillPlugin

class CalendarSkillPlugin(SkillPlugin):
    name = "calendar-skill"
    version = "0.1.0"
    description = "Calendar management capabilities"

    def skill_paths(self) -> list[Path]:
        return [Path(__file__).parent / "skills" / "calendar"]
```

**SKILL.md:**
Standard Claude Agent SDK skill definition with instructions, examples, etc.

## Skill Syncing

Skills are copied to the agent's session directory before the agent starts:

```
~/.claude/sessions/<session-id>/
├── skills/
│   ├── builtin-skill-1/         # Built-in skills
│   ├── builtin-skill-2/
│   ├── calendar/                # Plugin skill
│   └── voice-commands/          # Another plugin skill
```

The agent discovers and loads all skills in this directory.

## Implementation Steps

1. Define `SkillPlugin` base class in `plugin/skill.py`
2. Update `container_runner.py:_sync_skills()`:
   - Accept `PluginRegistry` parameter
   - After copying built-in skills, copy plugin skills
   - For each skill plugin:
     ```python
     for skill_path in plugin.skill_paths():
         dest = session_skills_dir / skill_path.name
         shutil.copytree(skill_path, dest)
     ```
3. Tests: skill discovery, copying, agent access

## Integration Points

- `container_runner.py:_sync_skills()` — copies plugin skills to session dir
- Agent session directory — skills discoverable by standard SDK mechanism
- No changes needed in agent runner — it already scans the skills directory

## Open Questions

- Should skills be able to declare dependencies on other skills?
- How to handle skill naming conflicts?
- Should plugin skills be able to override built-in skills?
- Do we need skill version compatibility checking?
- Should skills support templates or parameterization?

## Skill Naming Convention

To avoid conflicts, plugin skills should be prefixed with plugin name:
- Plugin `pynchy-plugin-foo` provides skill `foo-capability`
- Reduces collision risk with built-in or other plugin skills

## Verification

1. Create test skill plugin with simple SKILL.md
2. Install: `uv pip install -e /tmp/pynchy-plugin-test-skill`
3. Start agent, verify skill directory in session
4. Check agent can access and use the skill
5. Uninstall and verify skill no longer appears in new sessions
