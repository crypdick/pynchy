"""Agent-facing container paths shared by agent-runner components."""

from pathlib import Path

AGENT_HOME = Path("/home/agent")
AGENT_SOURCE_ROOT = AGENT_HOME / "src"
AGENT_WORKSPACE = AGENT_HOME / "workspace"
PYNCHY_SOURCE = AGENT_SOURCE_ROOT / "crypdick" / "pynchy"
