"""Explicit registration of built-in IPC handlers."""


def register_builtin_handlers() -> None:
    """Import built-in handler modules at the application composition root."""
    import pynchy.host.container_manager.ipc.handlers_ask_user  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_automations  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_deploy  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_groups  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_lifecycle  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_managed_feature  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_security  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_service  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_skills  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_source_health  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_task_status  # noqa: F401, PLC0415
    import pynchy.host.container_manager.ipc.handlers_tasks  # noqa: F401, PLC0415
