"""File-based IPC between host and containers."""

# Import handler modules to trigger self-registration in the registry.
import pynchy.ipc._handlers_ask_user  # noqa: F401
import pynchy.ipc._handlers_deploy  # noqa: F401
import pynchy.ipc._handlers_groups  # noqa: F401
import pynchy.ipc._handlers_lifecycle  # noqa: F401
import pynchy.ipc._handlers_service  # noqa: F401
import pynchy.ipc._handlers_tasks  # noqa: F401
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import dispatch
from pynchy.ipc._watcher import start_ipc_watcher

__all__ = [
    "IpcDeps",
    "dispatch",
    "start_ipc_watcher",
]
