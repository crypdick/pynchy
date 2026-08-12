"""Dependency-free identifiers shared by Pynchy domains and adapters."""

from typing import NewType

GroupFolder = NewType("GroupFolder", str)
RuntimeId = NewType("RuntimeId", str)
SessionId = NewType("SessionId", str)
ChatJid = NewType("ChatJid", str)
ChannelName = NewType("ChannelName", str)
OrphanReapAgeMs = NewType("OrphanReapAgeMs", int)
CapabilityId = NewType("CapabilityId", str)

_NON_EMPTY_REQUEST_ID_MESSAGE = "IPC request envelope request_id must be a non-empty string"
_UNSAFE_REQUEST_ID_MESSAGE = "IPC request envelope request_id must be a safe path component"


def validate_request_id(value: object) -> str:
    """Return one bounded request ID safe for use as a file name."""
    # NOTE: Update docs/architecture/ipc.md "Requests" if this contract changes.
    if not isinstance(value, str) or not value:
        raise ValueError(_NON_EMPTY_REQUEST_ID_MESSAGE)
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not value.isprintable()
        or len(value.encode()) > 128
    ):
        raise ValueError(_UNSAFE_REQUEST_ID_MESSAGE)
    return value
