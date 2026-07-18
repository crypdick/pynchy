"""Tests for host-side persistent learned-skill decisions."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.host.container_manager.ipc import registry
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
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_skills.find_learned_skill_dir",
            return_value=tmp_path / "obsidian-knowledge",
        ),
        patch(
            "pynchy.host.container_manager.ipc.write.get_settings",
            return_value=settings,
        ),
    ):
        await registry.dispatch(
            {
                "type": "skill_access:policy",
                "request_id": "request-1",
                "action": "grant_always",
                "skill_name": "obsidian-knowledge",
            },
            "pynchy-dev",
            True,
            NullIpcDeps(),
        )

    response = json.loads(
        (tmp_path / "ipc/pynchy-dev/responses/request-1.json").read_text(encoding="utf-8")
    )
    assert response == {
        "result": {
            "status": "error",
            "message": "Persistent skill decisions must be completed by an ask_user response.",
        }
    }
