"""BEFORE_TOOL_USE hook: block git push/pull/rebase inside containers.

Port of src/pynchy/agent/scripts/guard_git.sh. Agents must use the
sync_worktree_to_main MCP tool instead.
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.hooks import HookDecision

_BLOCKED_GIT_OPS = re.compile(r"\bgit\s+(push|pull|rebase)\b")
_RAW_HOST_REPO_MOUNT = "/danger/raw-host-repo-mount-prefer-your-worktree"

_REASON = (
    "Direct git push/pull/rebase is blocked. Use the sync_worktree_to_main "
    "tool instead — it coordinates with the host to publish your changes "
    "(either merging into main or opening a PR, depending on workspace policy). "
    "Commit your changes first, then call sync_worktree_to_main."
)
_WORKTREE_REASON = (
    "Do not work in the raw host checkout mount. Use the isolated worktree at "
    "/workspace/project instead; Pynchy coordinates publication through "
    "sync_worktree_to_main."
)


async def guard_git_hook(tool_name: str, tool_input: dict[str, Any]) -> HookDecision:
    """Block git push/pull/rebase in Bash. Allow everything else."""
    if tool_name != "Bash":
        return HookDecision(allowed=True)

    command = tool_input.get("command", "")
    if _RAW_HOST_REPO_MOUNT in command:
        return HookDecision(allowed=False, reason=_WORKTREE_REASON)
    if _BLOCKED_GIT_OPS.search(command):
        return HookDecision(allowed=False, reason=_REASON)

    return HookDecision(allowed=True)
