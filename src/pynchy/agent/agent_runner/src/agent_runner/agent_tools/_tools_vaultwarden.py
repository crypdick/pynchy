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

register_ipc_tool(
    name="manage_vaultwarden",
    description=(
        "Administer Vaultwarden without accepting secret values. Sources must be an existing "
        "vault item, browser-captured item, or protected host file."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "verify_access",
                    "upsert_item",
                    "set_item_collections",
                    "create_collection",
                    "set_channel_collections",
                ],
            },
            "name": {"type": "string", "minLength": 1, "maxLength": 256},
            "alias": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "channel": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "channels": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "collections": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "source_item": {"type": "string", "minLength": 1, "maxLength": 256},
            "source_file": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
