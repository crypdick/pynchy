"""Tests for host-side persistent learned-skill decisions."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy.host.container_manager.ipc.skill_access import persist_skill_access_choice


def test_always_choice_updates_the_workspace_profile() -> None:
    pending = {
        "source_group": "pynchy-dev",
        "questions": [{"skill_access": {"skill_name": "obsidian-knowledge"}}],
    }

    update_policy = MagicMock()
    status = persist_skill_access_choice(
        pending,
        {"answer": "Grant always"},
        profile_name_for_group=MagicMock(return_value="pynchy-dev"),
        update_profile_skill_policy=update_policy,
    )

    assert status == "granted"
    update_policy.assert_called_once_with("pynchy-dev", "obsidian-knowledge", grant=True)


@pytest.mark.asyncio
async def test_persistent_skill_action_requires_an_ask_user_callback(tmp_path) -> None:
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.write._ipc_base_dir",
            settings.data_dir / "ipc",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "action", "expected"),
    [
        ("unknown", "status", {"status": "unknown", "skill_name": "missing"}),
        ("granted", "status", {"status": "granted", "skill_name": "known"}),
        (
            "granted",
            "revoke",
            {"status": "error", "message": "Unknown skill access action: revoke"},
        ),
    ],
)
async def test_skill_access_policy_reports_status_and_action_errors(
    tmp_path, status: str, action: str, expected: dict[str, str]
) -> None:
    class Deps(NullIpcDeps):
        def skill_access_status(self, _group_folder: str, _skill_name: str) -> str:
            return status

    with patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"):
        await registry.dispatch(
            {
                "type": "skill_access:policy",
                "request_id": "request-1",
                "action": action,
                "skill_name": "missing" if status == "unknown" else "known",
            },
            "pynchy-dev",
            False,
            Deps(),
        )

    response = json.loads(
        (tmp_path / "ipc/pynchy-dev/responses/request-1.json").read_text(encoding="utf-8")
    )
    assert response == {"result": expected}


@pytest.mark.asyncio
async def test_skill_access_policy_ignores_malformed_requests(tmp_path) -> None:
    with patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"):
        await registry.dispatch(
            {
                "type": "skill_access:policy",
                "request_id": 1,
                "action": "status",
                "skill_name": "known",
            },
            "pynchy-dev",
            False,
            NullIpcDeps(),
        )

    assert not (tmp_path / "ipc/pynchy-dev/responses/1.json").exists()
