"""Boundary coverage for artifact security IPC payloads."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps

from pynchy import state
from pynchy.host.container_manager.ipc.handlers_artifact_security import (
    evaluate_package_coordinates,
    handle_artifact_security_check,
)
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.host.container_manager.security.package_metadata import (
    PackageCoordinate,
    PackageEcosystem,
    PackageIntent,
    PackageMetadataAssessment,
    PackageMetadataState,
    PackageSource,
)
from pynchy.workspace.api import WorkspaceProfile, WorkspaceSecurity
from tests.ipc_bash_security_support import _Deps


def _deps() -> _Deps:
    return _Deps(
        WorkspaceProfile(
            jid="discord:channel:1",
            name="Test",
            folder="test-ws",
            trigger="always",
        )
    )


async def _run_artifact_check(
    tmp_path,
    data: dict[str, object],
    gate: SecurityGate,
) -> dict[str, object]:
    await state.init_test_database()
    response_path = tmp_path / "response.json"
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.get_gate_for_group",
        return_value=gate,
    ):
        await handle_artifact_security_check(
            data,
            "test-ws",
            False,
            _deps(),
            response_path_override=response_path,
        )
    return json.loads(response_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_artifact_check_ignores_missing_request_id(tmp_path) -> None:
    await state.init_test_database()

    await handle_artifact_security_check(
        {"request_id": "", "tool_name": "Read"},
        "test-ws",
        False,
        NullIpcDeps(),
        response_path_override=tmp_path / "response.json",
    )

    assert not (tmp_path / "response.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("taint_evidence", [[1], [{"rule_id": "not-CRED001"}]])
async def test_malformed_taint_evidence_confirms_conservatively(
    tmp_path, taint_evidence: list[object]
) -> None:
    response = await _run_artifact_check(
        tmp_path,
        {
            "request_id": "malformed-taint",
            "tool_name": "Read",
            "file_access": True,
            "rule_ids": ["CRED001"],
            "taint_evidence": taint_evidence,
        },
        SecurityGate(WorkspaceSecurity()),
    )

    assert response["result"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_cop_disabled_confirms_credential_taint_without_inspection(tmp_path) -> None:
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.inspect_secret_taint",
        new_callable=AsyncMock,
    ) as inspect_taint:
        response = await _run_artifact_check(
            tmp_path,
            {
                "request_id": "cop-disabled",
                "tool_name": "Bash",
                "file_access": True,
                "rule_ids": ["CRED001"],
                "taint_evidence": [
                    {
                        "rule_id": "CRED001",
                        "artifact_kind": "command",
                        "artifact_value": "cat .env",
                    }
                ],
            },
            SecurityGate(WorkspaceSecurity(cop_active=False)),
        )

    assert response["result"]["decision"] == "allow"
    inspect_taint.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("packages", ["not-a-list", [{}]])
async def test_artifact_check_denies_malformed_package_payload(tmp_path, packages: object) -> None:
    response = await _run_artifact_check(
        tmp_path,
        {"request_id": "malformed-packages", "packages": packages},
        SecurityGate(WorkspaceSecurity()),
    )

    assert response["result"] == {
        "decision": "deny",
        "reason": "Malformed package coordinate payload",
        "guarded_action_id": "malformed-packages",
    }


@pytest.mark.asyncio
async def test_registry_package_without_name_is_denied() -> None:
    result, rule_ids = await evaluate_package_coordinates(
        (
            PackageCoordinate(
                PackageEcosystem.PYPI,
                None,
                "1.2.3",
                PackageSource.REGISTRY,
                PackageIntent.DEPENDENCY,
                False,
            ),
        )
    )

    assert result == {"decision": "deny", "reason": "Package name is missing"}
    assert rule_ids == ("PKG003",)


@pytest.mark.asyncio
async def test_degraded_package_metadata_is_recorded_in_audit_decision(tmp_path) -> None:
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.evaluate_package_coordinates",
        new_callable=AsyncMock,
        return_value=({"decision": "allow"}, ("PKG005",)),
    ):
        response = await _run_artifact_check(
            tmp_path,
            {"request_id": "degraded-package", "packages": []},
            SecurityGate(WorkspaceSecurity()),
        )

    assert response["result"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_locked_reconciliation_survives_degraded_package_metadata() -> None:
    coordinate = PackageCoordinate(
        PackageEcosystem.PYPI,
        "trusted-package",
        "1.2.3",
        PackageSource.REGISTRY,
        PackageIntent.RECONCILIATION,
        True,
    )

    async def degraded(_coordinate: PackageCoordinate) -> PackageMetadataAssessment:
        await asyncio.sleep(0)
        return PackageMetadataAssessment(PackageMetadataState.DEGRADED, "metadata unavailable")

    result, rule_ids = await evaluate_package_coordinates((coordinate,), assessor=degraded)

    assert result == {"decision": "allow"}
    assert rule_ids == ("PKG005",)


@pytest.mark.asyncio
async def test_degraded_non_reconciliation_package_metadata_needs_human_review() -> None:
    coordinate = PackageCoordinate(
        PackageEcosystem.PYPI,
        "unlocked-package",
        "1.2.3",
        PackageSource.REGISTRY,
        PackageIntent.DEPENDENCY,
        False,
    )

    async def degraded(_coordinate: PackageCoordinate) -> PackageMetadataAssessment:
        await asyncio.sleep(0)
        return PackageMetadataAssessment(PackageMetadataState.DEGRADED, "metadata unavailable")

    result, rule_ids = await evaluate_package_coordinates((coordinate,), assessor=degraded)

    assert result == {"decision": "needs_human", "reason": "metadata unavailable"}
    assert rule_ids == ("PKG005",)


@pytest.mark.asyncio
async def test_established_package_metadata_allows_a_non_static_coordinate() -> None:
    coordinate = PackageCoordinate(
        PackageEcosystem.PYPI,
        "established-package",
        "1.2.3",
        PackageSource.REGISTRY,
        PackageIntent.DEPENDENCY,
        False,
    )

    async def established(_coordinate: PackageCoordinate) -> PackageMetadataAssessment:
        await asyncio.sleep(0)
        return PackageMetadataAssessment(PackageMetadataState.ESTABLISHED, "known metadata")

    result, rule_ids = await evaluate_package_coordinates((coordinate,), assessor=established)

    assert result == {"decision": "allow"}
    assert rule_ids == ()
