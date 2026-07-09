"""Computer-use tool backed by host-side Cua Driver."""

from ._registry import register_ipc_tool

_ACTIONS = [
    "capture",
    "list_apps",
    "list_windows",
    "launch_app",
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "scroll",
    "wait",
    "check_permissions",
]

register_ipc_tool(
    name="computer_use",
    description=(
        "Drive the macOS host desktop through Cua Driver without attaching to a "
        "browser debugger. Use capture first, then act by element index or "
        "window-local screenshot coordinates. Returns text output and, for "
        "captures, screenshot paths readable under /workspace/ipc/computer-use."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Computer-use action to perform.",
            },
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Target application PID from launch_app or list_windows.",
            },
            "window_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Target window ID from launch_app or list_windows.",
            },
            "element": {
                "type": "integer",
                "minimum": 1,
                "description": "Element index from the latest capture/get_window_state output.",
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 2,
                "maxItems": 2,
                "description": "Window-local [x, y] pixel coordinate from a capture screenshot.",
            },
            "text": {
                "type": "string",
                "description": "Text for the type action.",
            },
            "keys": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": 'Shortcut for the key action, for example "cmd+s".',
            },
            "bundle_id": {
                "type": "string",
                "description": "Bundle ID for launch_app, for example com.google.Chrome.",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional URLs to open with launch_app.",
            },
            "query": {
                "type": "string",
                "description": "Optional accessibility-tree filter for capture.",
            },
            "seconds": {
                "type": "number",
                "minimum": 0,
                "default": 1,
                "description": "Delay for wait.",
            },
            "label": {
                "type": "string",
                "description": "Short label used for screenshot artifact filenames.",
            },
            "capture_after": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Run get_window_state after a state-changing action when "
                    "pid/window_id are known."
                ),
            },
        },
        "required": ["action"],
    },
)
