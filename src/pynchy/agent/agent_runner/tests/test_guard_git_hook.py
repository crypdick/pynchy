# src/pynchy/agent/agent_runner/tests/test_guard_git_hook.py

import pytest

from agent_runner.security.guard_git import guard_git_hook


class TestGuardGitHook:
    @pytest.mark.asyncio
    async def test_git_push_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git push origin main"})
        assert not d.allowed
        assert "sync_worktree_to_main" in d.reason

    @pytest.mark.asyncio
    async def test_git_pull_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git pull"})
        assert not d.allowed

    @pytest.mark.asyncio
    async def test_git_rebase_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git rebase origin/main"})
        assert not d.allowed

    @pytest.mark.asyncio
    async def test_git_status_allowed(self):
        d = await guard_git_hook("Bash", {"command": "git status"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_git_diff_allowed(self):
        d = await guard_git_hook("Bash", {"command": "git diff HEAD"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_raw_host_checkout_mount_blocked(self):
        d = await guard_git_hook(
            "Bash",
            {"command": "cd /danger/raw-host-repos/owner/project && git status"},
        )
        assert not d.allowed
        assert "/workspace/repos/<owner>/<repo>" in d.reason

    @pytest.mark.asyncio
    async def test_worktree_checkout_allowed(self):
        d = await guard_git_hook(
            "Bash", {"command": "cd /workspace/repos/owner/project && git status"}
        )
        assert d.allowed

    @pytest.mark.asyncio
    async def test_non_bash_tool_allowed(self):
        d = await guard_git_hook("Read", {"file_path": "/x"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_non_git_command_allowed(self):
        d = await guard_git_hook("Bash", {"command": "echo hello"})
        assert d.allowed
