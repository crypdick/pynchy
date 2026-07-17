"""Tests for host-side persistent learned-skill decisions."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import NullIpcDeps

from pynchy.host.container_manager.ipc import handlers_skills
from pynchy.host.container_manager.ipc.skill_access import persist_skill_access_choice


def test_always_choice_updates_the_workspace_profile() -> None:
    pending = {
        "source_group": "pynchy-dev",
        "questions": [{"skill_access": {"skill_name": "obsidian-knowledge"}}],
    }

    with (
        patch(
            "pynchy.host.container_manager.ipc.skill_access.profile_name_for_group",
            return_value="pynchy-dev",
        ),
        patch(
            "pynchy.host.container_manager.ipc.skill_access.update_profile_skill_policy"
        ) as update_policy,
    ):
        status = persist_skill_access_choice(pending, {"answer": "Grant always"})

    assert status == "granted"
    update_policy.assert_called_once_with("pynchy-dev", "obsidian-knowledge", grant=True)


@pytest.mark.asyncio
async def test_persistent_skill_action_requires_an_ask_user_callback(tmp_path) -> None:
    result: list[dict[str, object]] = []

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_skills.find_learned_skill_dir",
            return_value=tmp_path / "obsidian-knowledge",
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_skills._write_result",
            side_effect=lambda _group, _request, value: result.append(value),
        ),
    ):
        await handlers_skills._handle_skill_access(
            {
                "request_id": "request-1",
                "action": "grant_always",
                "skill_name": "obsidian-knowledge",
            },
            "pynchy-dev",
            True,
            NullIpcDeps(),
        )

    assert result == [
        {
            "status": "error",
            "message": "Persistent skill decisions must be completed by an ask_user response.",
        }
    ]
