"""Desktop screenshot tool backed by host-side macOS screencapture."""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="take_screenshot",
    description=(
        "Capture the macOS host desktop using the host's screencapture command. "
        "Returns host_path and container_path; the image is readable inside this "
        "container under /workspace/ipc/screenshots."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["full", "selection", "window"],
                "default": "full",
                "description": (
                    "Capture mode. full captures the display. selection and window "
                    "open macOS interactive selection UI on the host."
                ),
            },
            "label": {
                "type": "string",
                "description": "Short label used in the screenshot filename.",
            },
            "display_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional macOS display ID passed to screencapture -D.",
            },
            "include_cursor": {
                "type": "boolean",
                "default": False,
                "description": "Include the mouse cursor in the screenshot.",
            },
        },
    },
)
