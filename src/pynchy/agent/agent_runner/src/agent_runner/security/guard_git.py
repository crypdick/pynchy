"""Keep publication host-managed while allowing agents to resolve sync conflicts."""

from __future__ import annotations

import re
from typing import Any

from agent_runner.hooks import HookDecision

_BLOCKED_PUBLICATION_OPS = re.compile(r"\bgit\s+(push|pull)\b")
_GIT_REBASE = re.compile(r"\bgit\s+rebase\b")
_GIT_REBASE_RECOVERY = frozenset({"--continue", "--abort", "--skip"})
_RAW_HOST_REPO_MOUNT = "/danger/raw-host-repos/"

_REASON = (
    "Direct git push/pull or starting a rebase is blocked. Use the sync_worktree_to_main "
    "tool instead — it coordinates with the host to publish your changes as a pull request. "
    "You may resolve a conflict created by that tool with git rebase "
    "--continue, --abort, or --skip."
)
_WORKTREE_REASON = (
    "Do not work in the raw host checkout mount. Use the isolated worktree at "
    "/workspace/repos/<owner>/<repo> instead; Pynchy coordinates publication through "
    "sync_worktree_to_main."
)


def _starts_new_rebase(command: str) -> bool:
    for match in _GIT_REBASE.finditer(command):
        remainder = command[match.end() :].lstrip()
        first_argument = remainder.split(maxsplit=1)[0] if remainder else ""
        if first_argument not in _GIT_REBASE_RECOVERY:
            return True
    return False


async def guard_git_hook(  # noqa: RUF029 - async hook API.
    tool_name: str,
    tool_input: dict[str, Any],
) -> HookDecision:
    """Block direct publication and rebase initiation while permitting recovery."""
    if tool_name != "Bash":
        return HookDecision(allowed=True)

    command = tool_input.get("command", "")
    if _RAW_HOST_REPO_MOUNT in command:
        return HookDecision(allowed=False, reason=_WORKTREE_REASON)
    if _BLOCKED_PUBLICATION_OPS.search(command) or _starts_new_rebase(command):
        return HookDecision(allowed=False, reason=_REASON)

    return HookDecision(allowed=True)
