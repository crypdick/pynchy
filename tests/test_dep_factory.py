"""Runtime boundary tests for orchestrator dependency adapters."""

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.config_job_execution import ConfigJobExecutionDeps
from pynchy.host.orchestrator.dep_factory import make_http_deps, make_scheduler_deps
from pynchy.host.orchestrator.messaging.deps import MessageHandlerDeps
from pynchy.host.orchestrator.scheduled_turn import ScheduledTurnDeps
from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies
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


def test_scheduler_deps_share_app_contract_across_temporal_activities() -> None:
    app = PynchyApp()

    deps = make_scheduler_deps(app)

    assert deps is app
    assert deps.workspaces is app.workspaces
    assert isinstance(deps, SchedulerDependencies)
    assert isinstance(deps, ScheduledTurnDeps)
    assert isinstance(deps, ConfigJobExecutionDeps)
    assert isinstance(deps, MessageHandlerDeps)
