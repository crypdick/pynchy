# Plugin hook reference

Pynchy plugins implement hooks defined in `src/pynchy/plugins/hookspecs.py`.
Each hook represents one capability; a plugin can implement several.

All hooks use Pluggy's decorator:

```python
import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")
```

## Choose a hook category

| Category | Hooks |
|----------|-------|
| [Agent cores](agent-cores.md) | `pynchy_agent_core_info` |
| [Host services](host-services.md) | `pynchy_service_handler`, `pynchy_computer_use_backend`, `pynchy_action_specs` |
| [Skills](skills.md) | `pynchy_skill_paths` |
| [Channels and speech](channels.md) | `pynchy_create_channel`, `pynchy_speech_synthesizer` |
| [Connection runtimes](connections.md) | `pynchy_connection_runtime` |
| [Runtime and tunnels](runtime-and-tunnels.md) | `pynchy_container_runtime`, `pynchy_tunnel` |
| [Observers and memory](observers-and-memory.md) | `pynchy_observer`, `pynchy_memory` |
| [Webhooks](webhooks.md) | `pynchy_webhook_routes` |
| [MCP servers and workspaces](mcp-and-workspaces.md) | `pynchy_mcp_server_spec`, `pynchy_workspace_spec` |

## Multi-category plugins

One plugin can implement several hooks:

```python
class CalendarPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return CALENDAR_HOST_ACTIONS

    @hookimpl
    def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
        return CALENDAR_ACTION_SPECS

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return [str(Path(__file__).parent / "skills" / "calendar")]
```

No categories attribute is needed. Pluggy discovers capabilities from the hooks
the class implements.

## Hook execution order

Most plugins do not need ordering. Use a Pluggy ordering hint only when one
plugin must see or modify another plugin's result:

```python
@hookimpl(trylast=True)
@hookimpl(tryfirst=True)
```
