"""Curated container-runtime capabilities for composition and internal assurance."""

from pynchy.host.container_manager.gateway_builtin import BuiltinGateway
from pynchy.host.container_manager.ipc.handlers_artifact_security import (
    evaluate_package_coordinates,
    handle_artifact_security_check,
)
from pynchy.host.container_manager.mcp.canary_client import McpCanaryClient, McpCanaryToolError
from pynchy.host.container_manager.mcp.manager import get_mcp_manager
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
)
from pynchy.host.container_manager.security.cop_gate import cop_requires_human
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.host.container_manager.security.identity import (
    ReceiptVerification,
    clear_approval_receipts,
    consume_approval_receipt,
    guarded_action_id,
    issue_approval_receipt,
)
from pynchy.host.container_manager.security.package_metadata import (
    PackageCoordinate,
    PackageEcosystem,
    PackageIntent,
    PackageMetadataAssessment,
    PackageMetadataState,
    PackageSource,
    RegistryMetadataError,
    assess_package_metadata,
    clear_package_metadata_cache,
)
from pynchy.redaction import (
    GatewayRedactionPosture,
    redaction_posture_for_gateway_mode,
)

__all__ = [
    "BuiltinGateway",
    "CopContextAvailability",
    "CopInspectionContext",
    "CopVerdict",
    "GatewayRedactionPosture",
    "McpCanaryClient",
    "McpCanaryToolError",
    "PackageCoordinate",
    "PackageEcosystem",
    "PackageIntent",
    "PackageMetadataAssessment",
    "PackageMetadataState",
    "PackageSource",
    "ReceiptVerification",
    "RegistryMetadataError",
    "assess_package_metadata",
    "clear_approval_receipts",
    "clear_package_metadata_cache",
    "consume_approval_receipt",
    "cop_requires_human",
    "create_gate",
    "destroy_gate",
    "evaluate_package_coordinates",
    "get_mcp_manager",
    "guarded_action_id",
    "handle_artifact_security_check",
    "issue_approval_receipt",
    "redaction_posture_for_gateway_mode",
]
