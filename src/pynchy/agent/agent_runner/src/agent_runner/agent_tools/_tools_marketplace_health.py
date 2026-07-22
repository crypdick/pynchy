"""Aggregate-only marketplace health tool backed by the Pynchy host."""

from ._registry import register_ipc_tool

register_ipc_tool(
    name="marketplace_health_snapshot",
    description=(
        "Read marketplace pending and awaiting-reply counts plus mail-reader health. "
        "Returns no buyer, listing, or email content and does not mutate marketplace or mail state."
    ),
    input_schema={"type": "object", "properties": {}},
)
