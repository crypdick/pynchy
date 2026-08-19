"""Channel-scoped Vaultwarden secret retrieval."""

from ._registry import register_ipc_tool

register_ipc_tool(
    name="get_secret",
    description=(
        "Retrieve one exact-name login from this Discord channel's Vaultwarden collections. "
        "Returns a mode-0600 JSON file path and available keys, never secret values."
    ),
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 256}},
        "required": ["name"],
        "additionalProperties": False,
    },
)
