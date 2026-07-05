"""File-based IPC between host and containers."""

# Import handler modules to trigger self-registration in the registry.
import pynchy.host.container_manager.ipc.handlers_ask_user
import pynchy.host.container_manager.ipc.handlers_deploy
import pynchy.host.container_manager.ipc.handlers_groups
import pynchy.host.container_manager.ipc.handlers_lifecycle
import pynchy.host.container_manager.ipc.handlers_security
import pynchy.host.container_manager.ipc.handlers_service
import pynchy.host.container_manager.ipc.handlers_tasks  # noqa: F401
from pynchy.host.container_manager.ipc.deps import IpcDeps
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.ipc.watcher import start_ipc_watcher

__all__ = [
    "IpcDeps",
    "dispatch",
    "start_ipc_watcher",
]
