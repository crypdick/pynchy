"""Dependency-free identifiers shared by Pynchy domains and adapters."""

from typing import NewType

GroupFolder = NewType("GroupFolder", str)
RuntimeId = NewType("RuntimeId", str)
SessionId = NewType("SessionId", str)
ChatJid = NewType("ChatJid", str)
ChannelName = NewType("ChannelName", str)
OrphanReapAgeMs = NewType("OrphanReapAgeMs", int)
CapabilityId = NewType("CapabilityId", str)
