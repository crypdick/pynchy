# src/pynchy/agent/agent_runner/tests/test_guard_git_hook.py

import pytest

from agent_runner.security.guard_git import guard_git_hook


class TestGuardGitHook:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "pynchy publish-personalization",
            "uv run pynchy publish-personalization",
            "uv run pynchy 'publish-personalization'",
            "python -m pynchy publish-personalization",
            "python3.12 -m pynchy publish-personalization",
            "uv run python -m pynchy publish-personalization",
            "python -m pynchy.__main__ publish-personalization",
            "uv run python -m pynchy.__main__ publish-personalization",
            "uvx pynchy publish-personalization",
            "sh -c 'pynchy publish-personalization'",
            "sh -c 'sh -c \"pynchy publish-personalization\"'",
        ],
    )
    async def test_host_personalization_publish_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "host operator" in decision.reason

    @pytest.mark.asyncio
    async def test_git_push_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git push origin main"})
        assert not d.allowed
        assert "sync_worktree_to_main" in d.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "git p",
            "git external-publish",
            "git config alias.p '!git push origin main' && git p",
            "git -c alias.p='push origin main' p",
        ],
    )
    async def test_git_alias_or_external_invocation_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "sync_worktree_to_main" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "command git push origin main",
            "sh -c 'git push origin main'",
            "uv run git push origin main",
        ],
    )
    async def test_wrapped_git_push_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "sync_worktree_to_main" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "env -S 'git push origin main'",
            "env -S 'pynchy publish-personalization'",
            "nice env -S 'git push origin main'",
            "timeout 5 env -S 'git push origin main'",
            "time env -S 'pynchy publish-personalization'",
        ],
    )
    async def test_split_environment_command_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "generated commands" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "git bisect run git push origin main",
            "git filter-branch --env-filter 'git push origin main'",
            "git difftool --extcmd='git push origin main'",
            "git for-each-repo --config=repo.group git push origin main",
            "git submodule foreach 'git push origin main'",
        ],
    )
    async def test_git_command_execution_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "sync_worktree_to_main" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            ". publish.sh",
            "source publish.sh",
            "bash publish.sh",
            "sh publish.sh",
        ],
    )
    async def test_uninspected_shell_script_execution_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "script content" in decision.reason

    @pytest.mark.asyncio
    async def test_git_pull_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git pull"})
        assert not d.allowed

    @pytest.mark.asyncio
    async def test_git_rebase_blocked(self):
        d = await guard_git_hook("Bash", {"command": "git rebase origin/main"})
        assert not d.allowed

    @pytest.mark.asyncio
    async def test_nested_git_rebase_blocked(self):
        decision = await guard_git_hook("Bash", {"command": "bash -c 'git rebase origin/main'"})

        assert not decision.allowed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["continue", "abort", "skip"])
    async def test_sync_conflict_recovery_is_allowed(self, action):
        d = await guard_git_hook("Bash", {"command": f"git rebase --{action}"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_recovery_cannot_hide_a_second_new_rebase(self):
        d = await guard_git_hook(
            "Bash",
            {"command": "git rebase --continue && git rebase origin/main"},
        )
        assert not d.allowed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "if true; then git push origin main; fi",
            "for item in one; do git push origin main; done",
            "while false; do git pull; done",
            "time git push origin main",
            "! git rebase origin/main",
            "exec git push origin main",
            "nice -n 1 git push origin main",
            "timeout 5 git push origin main",
            "nohup git pull",
        ],
    )
    async def test_control_words_and_wrappers_cannot_bypass_git_guard(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "sync_worktree_to_main" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "if true; then pynchy publish-personalization; fi",
            "time pynchy publish-personalization",
            "time sh -c 'pynchy publish-personalization'",
        ],
    )
    async def test_control_words_and_wrappers_cannot_bypass_personalization_guard(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "host operator" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            'echo "$(git push origin main)"',
            'echo "don\'t $(git push origin main)"',
            'echo "$(pynchy publish-personalization)"',
            "sh -c 'echo \"$(git push origin main)\"'",
            "sh -c 'echo \"$(pynchy publish-personalization)\"'",
            'eval "git push origin main"',
            'time eval "git push origin main"',
            "`git pull`",
            "runner=pynchy; $runner publish-personalization",
            "git_runner=git; $git_runner push origin main",
            "sh -c 'runner=pynchy; \"$runner\" publish-personalization'",
            "runner=git; nice $runner push origin main",
            "runner=git; timeout 5 $runner push origin main",
            "runner=git; nohup $runner push origin main",
            "runner=pynchy; nice $runner publish-personalization",
            "runner=pynchy; timeout 5 $runner publish-personalization",
            'module=pynchy; python -m "$module" publish-personalization',
            'module=pynchy.__main__; uv run python -m "$module" publish-personalization',
        ],
    )
    async def test_dynamic_shell_execution_is_blocked(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "generated commands" in decision.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "pynchy publish-personalization>/tmp/personalization-output",
            "pynchy 2>/tmp/personalization-output publish-personalization",
            "python -m pynchy publish-personalization<<<input",
            "python -m pynchy 2>/tmp/output publish-personalization",
            "uv run python -m pynchy.__main__ publish-personalization 2>/tmp/output",
        ],
    )
    async def test_redirection_cannot_hide_personalization_publication(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert not decision.allowed
        assert "host operator" in decision.reason

    @pytest.mark.asyncio
    async def test_nonpublication_redirection_is_allowed(self):
        decision = await guard_git_hook("Bash", {"command": "echo hello >/tmp/output"})

        assert decision.allowed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            'echo "$HOME"',
            'git -C "$HOME" status',
            'printf "%s\\n" "$PYNCHY_SKILLS_ROOT"',
        ],
    )
    async def test_parameter_expansion_in_arguments_is_allowed(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert decision.allowed

    @pytest.mark.asyncio
    async def test_git_status_allowed(self):
        d = await guard_git_hook("Bash", {"command": "git status"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_git_diff_allowed(self):
        d = await guard_git_hook("Bash", {"command": "git diff HEAD"})
        assert d.allowed

    @pytest.mark.asyncio
    async def test_non_foreach_submodule_command_allowed(self):
        decision = await guard_git_hook("Bash", {"command": "git submodule status"})

        assert decision.allowed

    @pytest.mark.asyncio
    async def test_nested_git_status_allowed(self):
        decision = await guard_git_hook("Bash", {"command": "bash -c 'git status'"})

        assert decision.allowed

    @pytest.mark.asyncio
    async def test_git_config_remains_allowed(self):
        decision = await guard_git_hook("Bash", {"command": "git config --get alias.p"})

        assert decision.allowed

    @pytest.mark.asyncio
    async def test_raw_host_checkout_mount_blocked(self):
        d = await guard_git_hook(
            "Bash",
            {"command": "cd /danger/raw-host-repos/owner/project && git status"},
        )
        assert not d.allowed
        assert "/home/agent/src/<owner>/<repo>" in d.reason

    @pytest.mark.asyncio
    async def test_worktree_checkout_allowed(self):
        d = await guard_git_hook(
            "Bash", {"command": "cd /home/agent/src/owner/project && git status"}
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "echo 'pynchy publish-personalization'",
            "rg 'pynchy publish-personalization' docs",
            "echo 'git push origin main'",
            "echo 'git p'",
            "echo '$(git push origin main)'",
            "echo '`git push origin main`'",
            "echo 'eval git push origin main'",
            "echo '$runner publish-personalization'",
        ],
    )
    async def test_publication_text_is_not_an_invocation(self, command):
        decision = await guard_git_hook("Bash", {"command": command})

        assert decision.allowed
