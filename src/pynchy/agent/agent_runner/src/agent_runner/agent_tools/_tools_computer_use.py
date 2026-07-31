"""Backend-neutral computer-use tool mediated by the Pynchy host."""

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
    "set_value",
    "perform_action",
    "menu_list",
    "menu_click",
    "dialog_list",
    "dialog_click",
    "dialog_input",
    "dialog_file",
    "dialog_dismiss",
    "clipboard_get",
    "clipboard_set",
    "clipboard_clear",
    "clipboard_save",
    "clipboard_restore",
    "space_list",
    "space_switch",
    "space_move_window",
    "wait",
    "check_permissions",
]

register_ipc_tool(
    name="computer_use",
    description=(
        "Inspect and operate the host desktop through a configured provider plugin. "
        "Capture first, preserve returned snapshot and element IDs exactly, then act "
        "through the same policy-enforced host tool. Screenshot artifacts appear under "
        "/run/pynchy/computer-use."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Computer-use operation to perform.",
            },
            "app": {
                "type": "string",
                "minLength": 1,
                "description": "Target application name or bundle identifier.",
            },
            "bundle_id": {
                "type": "string",
                "minLength": 1,
                "description": "Bundle identifier for launch_app.",
            },
            "pid": {
                "type": "integer",
                "minimum": 1,
                "description": "Target application process ID.",
            },
            "window_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Target window's CoreGraphics identifier.",
            },
            "window_title": {
                "type": "string",
                "minLength": 1,
                "description": "Target window title or partial title.",
            },
            "window_index": {
                "type": "integer",
                "minimum": 0,
                "description": "Target window index returned by list_windows.",
            },
            "snapshot_id": {
                "type": "string",
                "minLength": 1,
                "description": "Opaque snapshot ID returned by capture.",
            },
            "element": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "integer", "minimum": 1},
                ],
                "description": (
                    "Opaque provider element ID from capture, or a numeric Cua fallback index."
                ),
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Semantic element query when no exact element ID is available.",
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 2,
                "maxItems": 2,
                "description": "Target-relative [x, y] coordinate.",
            },
            "text": {
                "type": "string",
                "description": "Text for type, dialog_input, or clipboard_set.",
            },
            "value": {
                "type": "string",
                "description": "Accessibility value for set_value.",
            },
            "keys": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                ],
                "description": 'Shortcut for key, for example "cmd+s".',
            },
            "accessibility_action": {
                "type": "string",
                "minLength": 1,
                "description": "Named accessibility action such as AXPress or AXIncrement.",
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": "Direction for scroll.",
            },
            "amount": {
                "type": "integer",
                "minimum": 1,
                "description": "Scroll amount in provider ticks.",
            },
            "delta_y": {
                "type": "integer",
                "description": "Cua-compatible vertical scroll delta.",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "URLs or paths to open during launch_app.",
            },
            "menu_item": {
                "type": "string",
                "minLength": 1,
                "description": "Single menu item for menu_click.",
            },
            "menu_path": {
                "type": "string",
                "minLength": 1,
                "description": 'Nested menu path such as "File > Export > PDF".',
            },
            "button": {
                "type": "string",
                "minLength": 1,
                "description": "Button label for dialog_click.",
            },
            "field": {
                "type": "string",
                "minLength": 1,
                "description": "Field label for dialog_input.",
            },
            "index": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based dialog field index.",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Directory path for dialog_file.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Filename for dialog_file.",
            },
            "select": {
                "type": "string",
                "minLength": 1,
                "description": "Confirmation button for dialog_file.",
            },
            "slot": {
                "type": "string",
                "minLength": 1,
                "description": "Named clipboard save/restore slot.",
            },
            "prefer": {
                "type": "string",
                "minLength": 1,
                "description": "Preferred clipboard UTI for clipboard_get.",
            },
            "space": {
                "type": "integer",
                "minimum": 1,
                "description": "One-based macOS Space number.",
            },
            "seconds": {
                "type": "number",
                "minimum": 0,
                "default": 1,
                "description": "Delay for wait.",
            },
            "label": {
                "type": "string",
                "description": "Short screenshot artifact label.",
            },
            "capture_after": {
                "type": "boolean",
                "default": False,
                "description": "Capture a semantic snapshot after a mutating action.",
            },
            "foreground": {
                "type": "boolean",
                "default": False,
                "description": "Focus the target before synthetic input when supported.",
            },
            "clear": {"type": "boolean", "default": False},
            "smooth": {"type": "boolean", "default": False},
            "verify": {"type": "boolean", "default": False},
            "include_disabled": {"type": "boolean", "default": False},
            "detailed": {"type": "boolean", "default": False},
            "ensure_expanded": {"type": "boolean", "default": False},
            "force": {"type": "boolean", "default": False},
            "follow": {"type": "boolean", "default": False},
            "to_current": {"type": "boolean", "default": False},
            "wait_until_ready": {"type": "boolean", "default": False},
            "no_focus": {"type": "boolean", "default": False},
        },
        "required": ["action"],
    },
)
