"""Idempotency ledger for host-mutating IPC requests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pynchy.host.container_manager.ipc.protocol import (
    IpcRequestEnvelope,
    request_requires_idempotency_ledger,
)
from pynchy.logger import logger


def claim_request_for_execution(envelope: IpcRequestEnvelope, ipc_base_dir: Path) -> bool:
    """Create the idempotency ledger entry for a host-mutating request.

    The ledger file is created with O_EXCL before dispatch, so duplicate
    deliveries of the same request ID cannot execute a second host mutation.
    """
    if not request_requires_idempotency_ledger(envelope.kind):
        return True

    ledger_dir = ipc_base_dir / envelope.source_group / "request_ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_file = ledger_dir / f"{envelope.request_id}.json"
    try:
        fd = os.open(ledger_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        logger.info(
            "Duplicate IPC request skipped",
            request_id=envelope.request_id,
            kind=envelope.kind,
            source_group=envelope.source_group,
        )
        return False

    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "schema_version": envelope.schema_version,
                "kind": envelope.kind,
                "request_id": envelope.request_id,
                "source_group": envelope.source_group,
                "created_at": envelope.created_at,
                "deadline": envelope.deadline,
                "status": "claimed",
            },
            f,
            indent=2,
        )
    return True
