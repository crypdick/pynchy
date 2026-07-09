"""Desktop screenshot tool backed by host-side macOS screencapture."""

from ._registry import register_ipc_tool

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

register_ipc_tool(
    name="analyze_screenshot",
    description=(
        "Analyze a PNG captured by take_screenshot using the host LLM gateway. "
        "Pass image_path from take_screenshot's container_path, or omit image_path "
        "to analyze the newest screenshot in /workspace/ipc/screenshots."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": (
                    "Screenshot path returned by take_screenshot, usually "
                    "/workspace/ipc/screenshots/<filename>.png. If omitted, analyzes "
                    "the latest screenshot for this workspace."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "Question or instruction for analyzing the screenshot.",
            },
            "model": {
                "type": "string",
                "description": "Optional LiteLLM/OpenAI-compatible vision model route.",
            },
            "max_output_tokens": {
                "type": "integer",
                "minimum": 1,
                "default": 1200,
                "description": "Maximum tokens to return from the vision model.",
            },
        },
    },
)
