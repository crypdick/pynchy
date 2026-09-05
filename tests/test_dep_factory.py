"""Runtime boundary tests for orchestrator dependency adapters."""

from typing import get_type_hints

from conftest import NullChannel

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.config_job_execution import ConfigJobExecutionDeps
from pynchy.host.orchestrator.dep_factory import make_http_deps, make_ipc_deps, make_scheduler_deps
from pynchy.host.orchestrator.messaging.deps import MessageHandlerDeps
from pynchy.host.orchestrator.scheduled_turn import ScheduledTurnDeps
from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies
from pynchy.plugins.api import HostActionDescriptor
from pynchy.workspace.api import WorkspaceProfile


def test_http_deps_resolve_workspace_annotations_at_runtime() -> None:
    app = PynchyApp()
    workspace = WorkspaceProfile(
        jid="discord:channel:linear",
        name="Linear",
        folder="linear",
        trigger="@Pynchy",
    )
    app.workspaces[workspace.jid] = workspace

    assert make_http_deps(app).get_workspace(workspace.folder) is workspace


def test_http_capability_policy_annotations_resolve_at_runtime() -> None:
    evaluate = make_http_deps(PynchyApp()).capability_status_operations.evaluate_action_policy

    assert get_type_hints(evaluate)["action"] is HostActionDescriptor


def test_scheduler_deps_share_app_contract_across_temporal_activities() -> None:
    app = PynchyApp()

    deps = make_scheduler_deps(app)

    assert deps is app
    assert deps.workspaces is app.workspaces
    assert isinstance(deps, SchedulerDependencies)
    assert isinstance(deps, ScheduledTurnDeps)
    assert isinstance(deps, ConfigJobExecutionDeps)
    assert isinstance(deps, MessageHandlerDeps)


async def test_ipc_refresh_syncs_only_channels_with_metadata_support() -> None:
    class SyncingChannel(NullChannel):
        def __init__(self) -> None:
            self.forces: list[bool] = []

        async def sync_group_metadata(self, *, force: bool) -> None:
            self.forces.append(force)

    app = PynchyApp()
    first, second = SyncingChannel(), SyncingChannel()
    app.channels = [first, NullChannel(), second]
    deps = make_ipc_deps(app)

    await deps.sync_group_metadata(force=True)
    await deps.sync_group_metadata(force=False)

    assert first.forces == [True, False]
    assert second.forces == [True, False]
