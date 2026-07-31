"""Regression coverage for direct-host routed publication authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.container_manager.ipc.handlers_lifecycle import PublicationRepositoryError
from pynchy.host.git_ops.api import RepoContext
from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.app import PynchyApp

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _ResolvedHostWorkspace:
    execution_mode: str
    cwd: str
    repo: list[str]


class TestPublicationRepositorySelection:
    """Verify lifecycle publication keeps direct-host routing host-owned."""

    def test_malformed_routed_host_repository_access_blocks_execution(self, tmp_path: Path) -> None:
        folder = "host__thread_conversation-conv_malformed"
        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime"):
            app = PynchyApp()

        with (
            patch(
                "pynchy.host.orchestrator.app.get_repo_context",
                side_effect=ValueError("repository slug must contain one slash"),
            ) as get_repo_context,
            pytest.raises(
                host_execution.HostExecutionCwdError,
                match="malformed repository access",
            ),
        ):
            app.host_runtime_operations.resolve_routed_host_cwd(
                folder,
                tmp_path,
                ["pynchy"],
                recovered=False,
            )

        get_repo_context.assert_called_once_with("pynchy")

    def test_routed_host_uses_active_scheduled_repository_override(self, tmp_path: Path) -> None:
        folder = "host__thread_conversation-conv_override"
        override_slug = "owner/override"
        active_turn_id = "turn-active"
        override_repo = RepoContext(
            slug=override_slug,
            root=tmp_path / "override",
            worktrees_dir=tmp_path / "worktrees" / "override",
        )

        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime") as configure:
            PynchyApp()
        runtime = configure.call_args.args[0]

        with (
            patch(
                "pynchy.host.orchestrator.app.load_resolved_config",
                return_value=_ResolvedHostWorkspace(
                    "host",
                    str(tmp_path / "configured-parent"),
                    ["owner/profile-repository"],
                ),
            ),
            patch(
                "pynchy.host.orchestrator.app.get_repo_context",
                return_value=override_repo,
            ) as get_repo_context,
            patch(
                "pynchy.host.orchestrator.app.resolve_repos_for_group",
                side_effect=AssertionError("routed publication must not reread profile access"),
            ) as resolve_repos_for_group,
        ):
            host_execution.bind_active_routed_host_repo(folder, override_slug, active_turn_id)
            try:
                assert runtime.resolve_publication_repos(folder, active_turn_id) == (override_repo,)
                with pytest.raises(PublicationRepositoryError, match="does not match"):
                    runtime.resolve_publication_repos(folder, "turn-stale")
                host_execution.clear_active_routed_host_repo(folder, override_slug, active_turn_id)
                with pytest.raises(PublicationRepositoryError, match="no longer active"):
                    runtime.resolve_publication_repos(folder, active_turn_id)
            finally:
                host_execution.clear_active_routed_host_repo(folder, override_slug, active_turn_id)

        get_repo_context.assert_called_once_with(override_slug)
        resolve_repos_for_group.assert_not_called()

    def test_active_route_stays_bound_after_workspace_configuration_changes(
        self, tmp_path: Path
    ) -> None:
        folder = "host__thread_conversation-conv_changed"
        route_slug = "owner/routed"
        turn_id = "turn-active"
        route_repo = RepoContext(
            slug=route_slug,
            root=tmp_path / "routed",
            worktrees_dir=tmp_path / "worktrees" / "routed",
        )

        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime") as configure:
            PynchyApp()
        runtime = configure.call_args.args[0]

        with (
            patch(
                "pynchy.host.orchestrator.app.load_resolved_config",
                return_value=None,
            ),
            patch(
                "pynchy.host.orchestrator.app.get_repo_context",
                return_value=route_repo,
            ) as get_repo_context,
            patch(
                "pynchy.host.orchestrator.app.resolve_repos_for_group",
                side_effect=AssertionError("active route must not fall back to profile access"),
            ) as resolve_repos_for_group,
        ):
            host_execution.bind_active_routed_host_repo(folder, route_slug, turn_id)
            try:
                assert runtime.resolve_publication_repos(folder, turn_id) == (route_repo,)
            finally:
                host_execution.clear_active_routed_host_repo(folder, route_slug, turn_id)

        get_repo_context.assert_called_once_with(route_slug)
        resolve_repos_for_group.assert_not_called()

    def test_stale_route_without_workspace_configuration_stays_blocked(self) -> None:
        folder = "host__thread_conversation-conv_deleted"
        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime") as configure:
            PynchyApp()
        runtime = configure.call_args.args[0]

        with (
            patch("pynchy.host.orchestrator.app.load_resolved_config", return_value=None),
            patch(
                "pynchy.host.orchestrator.app.resolve_repos_for_group",
                side_effect=AssertionError("stale route must not fall back to profile access"),
            ) as resolve_repos_for_group,
            pytest.raises(PublicationRepositoryError, match="no active workspace"),
        ):
            runtime.resolve_publication_repos(folder, "turn-stale")

        resolve_repos_for_group.assert_not_called()

    @pytest.mark.parametrize("lookup_raises", [False, True])
    def test_active_route_without_a_resolvable_repository_stays_blocked(
        self, lookup_raises: bool
    ) -> None:
        folder = "host__thread_conversation-conv_missing_repo"
        route_slug = "owner/missing"
        turn_id = "turn-active"
        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime") as configure:
            PynchyApp()
        runtime = configure.call_args.args[0]

        lookup = patch(
            "pynchy.host.orchestrator.app.get_repo_context",
            side_effect=ValueError("unknown repository") if lookup_raises else None,
            return_value=None,
        )

        host_execution.bind_active_routed_host_repo(folder, route_slug, turn_id)
        try:
            with lookup, pytest.raises(PublicationRepositoryError, match="unavailable repository"):
                runtime.resolve_publication_repos(folder, turn_id)
        finally:
            host_execution.clear_active_routed_host_repo(folder, route_slug, turn_id)

    def test_unrouted_publication_falls_back_to_workspace_repositories(
        self, tmp_path: Path
    ) -> None:
        folder = "group"
        repo = RepoContext(
            slug="owner/repo",
            root=tmp_path / "repo",
            worktrees_dir=tmp_path / "worktrees",
        )
        with patch("pynchy.host.orchestrator.app.configure_lifecycle_runtime") as configure:
            PynchyApp()
        runtime = configure.call_args.args[0]

        with (
            patch("pynchy.host.orchestrator.app.load_resolved_config", return_value=None),
            patch(
                "pynchy.host.orchestrator.app.resolve_repos_for_group",
                return_value=(repo,),
            ) as resolve_repos_for_group,
        ):
            assert runtime.resolve_publication_repos(folder, None) == (repo,)

        resolve_repos_for_group.assert_called_once_with(folder)
