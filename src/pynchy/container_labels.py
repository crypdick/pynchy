"""Container labels used by host-managed agent containers."""

AGENT_CONTAINER_LABEL = "com.pynchy.role"
AGENT_CONTAINER_LABEL_VALUE = "agent"

# Provenance separates resources created by the runtime test harness from
# production ones. Container names are folder-derived in both cases and can look
# alike, so the reaper keys on this label instead of on name shape.
PROVENANCE_LABEL = "com.pynchy.provenance"
PROVENANCE_LABEL_VALUE_TEST = "test"

# Namespace recovers the resource grouping when harness state is lost, which is
# what strands resources permanently otherwise.
NAMESPACE_LABEL = "com.pynchy.namespace"

# Ownership lets the reaper distinguish an abandoned resource from one a
# concurrently running suite still needs.
OWNER_PID_LABEL = "com.pynchy.owner-pid"
OWNER_BOOT_LABEL = "com.pynchy.owner-boot"
