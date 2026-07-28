"""Semantic action declarations for the host computer-use surface."""

from __future__ import annotations

from pynchy.actions._contract import ActionSpec, ActionSurface, ActionTransport
from pynchy.actions._spec_helpers import build_action

_COMPUTER_USE_ACTIONS = (
    ("desktop.computer.capture", "capture", "Capture a semantic desktop snapshot."),
    ("desktop.computer.app.list", "list_apps", "List desktop applications."),
    ("desktop.computer.window.list", "list_windows", "List desktop windows."),
    ("desktop.computer.app.launch", "launch_app", "Launch a desktop application."),
    ("desktop.computer.click", "click", "Click a desktop element or coordinate."),
    (
        "desktop.computer.double.click",
        "double_click",
        "Double-click a desktop element or coordinate.",
    ),
    (
        "desktop.computer.right.click",
        "right_click",
        "Right-click a desktop element or coordinate.",
    ),
    ("desktop.computer.text.type", "type", "Type text into a desktop application."),
    ("desktop.computer.key.send", "key", "Send a keyboard shortcut."),
    ("desktop.computer.scroll", "scroll", "Scroll a desktop application."),
    (
        "desktop.computer.element.value.set",
        "set_value",
        "Set an accessibility element value.",
    ),
    (
        "desktop.computer.element.action.perform",
        "perform_action",
        "Perform a named accessibility action.",
    ),
    ("desktop.computer.menu.list", "menu_list", "List application menu items."),
    ("desktop.computer.menu.click", "menu_click", "Click an application menu item."),
    ("desktop.computer.dialog.list", "dialog_list", "List the active system dialog."),
    ("desktop.computer.dialog.click", "dialog_click", "Click a system dialog button."),
    ("desktop.computer.dialog.input", "dialog_input", "Enter text in a system dialog."),
    ("desktop.computer.dialog.file", "dialog_file", "Operate a system file dialog."),
    (
        "desktop.computer.dialog.dismiss",
        "dialog_dismiss",
        "Dismiss the active system dialog.",
    ),
    ("desktop.computer.clipboard.get", "clipboard_get", "Read the host clipboard."),
    ("desktop.computer.clipboard.set", "clipboard_set", "Set host clipboard text."),
    ("desktop.computer.clipboard.clear", "clipboard_clear", "Clear the host clipboard."),
    ("desktop.computer.clipboard.save", "clipboard_save", "Save the host clipboard."),
    (
        "desktop.computer.clipboard.restore",
        "clipboard_restore",
        "Restore a saved host clipboard.",
    ),
    ("desktop.computer.space.list", "space_list", "List macOS Spaces."),
    ("desktop.computer.space.switch", "space_switch", "Switch macOS Spaces."),
    (
        "desktop.computer.space.window.move",
        "space_move_window",
        "Move a window between macOS Spaces.",
    ),
    ("desktop.computer.wait", "wait", "Wait for the desktop to settle."),
    (
        "desktop.computer.permissions.check",
        "check_permissions",
        "Check host permissions for computer use.",
    ),
)

COMPUTER_USE_ACTION_SPECS: tuple[ActionSpec, ...] = tuple(
    build_action(
        action_id,
        "computer-use",
        summary,
        ActionSurface(ActionTransport.AGENT_TOOL, "computer_use", discriminator),
        canary="desktop.computer.round.trip",
    )
    for action_id, discriminator, summary in _COMPUTER_USE_ACTIONS
)
