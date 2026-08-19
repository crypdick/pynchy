"""Stable host and container filesystem paths shared across adapters."""

from pathlib import Path

# Agent-facing container paths stay under the agent home. Harness-only paths use
# FHS-style roots so the agent workspace and runner plumbing remain distinct.
AGENT_HOME_CONTAINER_PATH = "/home/agent"
AGENT_SOURCE_CONTAINER_ROOT = f"{AGENT_HOME_CONTAINER_PATH}/src"
AGENT_WORKSPACE_CONTAINER_PATH = f"{AGENT_HOME_CONTAINER_PATH}/workspace"
AGENT_SKILLS_CONTAINER_PATH = f"{AGENT_HOME_CONTAINER_PATH}/skills"
AGENT_MEMORY_CONTAINER_PATH = f"{AGENT_HOME_CONTAINER_PATH}/memory"
AGENT_AUTOMATION_MEMORY_CONTAINER_PATH = f"{AGENT_HOME_CONTAINER_PATH}/automation-memory"
AGENT_EXTRA_MOUNT_CONTAINER_ROOT = f"{AGENT_HOME_CONTAINER_PATH}/mnt"
PYNCHY_IPC_CONTAINER_PATH = "/run/pynchy"
PYNCHY_SECRETS_CONTAINER_PATH = "/tmp/pynchy-secrets"  # noqa: S108 - agent contract.
PYNCHY_SCRIPTS_CONTAINER_PATH = "/opt/pynchy/scripts"
PYNCHY_AGENT_RUNNER_CONTAINER_PATH = "/opt/pynchy/agent-runner/src"
PYNCHY_PLUGIN_HOOK_ROOT = "/opt/pynchy/plugin-hooks"

# NOTE: Update docs/usage/personalization.md if this canonical publication target changes.
PERSONALIZATION_RELATIVE_DIR = Path("data/personalization")
PERSONALIZATION_SKILLS_CONTAINER_PATH = AGENT_SKILLS_CONTAINER_PATH
SKILLS_DIRNAME = "skills"
