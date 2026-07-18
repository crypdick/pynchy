# Peekaboo computer-use plugin

## Goal

Add a host-side macOS computer-use backend powered by Peekaboo's accessibility
API, while preserving Pynchy's IPC, approval, and workspace-isolation model.

## Scope

- Define a plugin-owned configuration and capability contract for Peekaboo.
- Expose semantic snapshots, stable element references, window/application
  targeting, typed text entry, actions, menus, dialogs, clipboard, and Spaces.
- Keep the existing Cua Driver backend as a fallback where Peekaboo is not
  available.
- Verify the permission and failure paths on a real macOS host.

## Boundary

Do not mount the raw Peekaboo CLI or its host permissions into agent
containers, and do not let a skill install Homebrew dependencies itself. The
plugin must execute on the host through the existing policy-enforced computer
use surface so every workspace retains its source-group attribution and normal
approval rules.
