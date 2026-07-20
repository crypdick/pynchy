"""Runtime boundary tests for orchestrator dependency adapters."""

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.types import WorkspaceProfile


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
