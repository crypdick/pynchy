"""Tests for host-side agent session id tracking."""

from pynchy.host.orchestrator.agent_runner import session_id_from_output
from pynchy.types import ContainerOutput


def test_session_id_from_output_reads_system_event_metadata():
    output = ContainerOutput(
        status="success",
        type="system",
        system_subtype="thread.started",
        system_data={"session_id": "codex:thread-1"},
    )

    assert session_id_from_output(output) == "codex:thread-1"
