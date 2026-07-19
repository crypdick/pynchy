# Desktop Screenshots

Pynchy exposes desktop screenshot tools for macOS hosts.

`take_screenshot` runs `/usr/sbin/screencapture` in the host process and saves
the PNG under the requesting workspace's IPC directory:

- Host path: `data/ipc/<workspace>/screenshots/<timestamp>-<label>.png`
- Container path: `/workspace/ipc/screenshots/<timestamp>-<label>.png`

The container path is returned in the tool result so the agent can inspect or
refer to the captured image without needing direct access to arbitrary host
folders.

`analyze_screenshot` sends one of those PNGs to the configured LLM gateway as a
vision request and returns a text analysis. Pass the `container_path` returned by
`take_screenshot`, or omit `image_path` to analyze the newest screenshot in the
workspace screenshot directory. The default model is `[agent].model`; pass
`model` to use a specific LiteLLM route.

## macOS permissions

macOS requires Screen Recording permission for the process that runs Pynchy. If
Pynchy runs under launchd, grant permission to the executable or terminal/session
that owns the service. If permission is missing, `screencapture` returns a
failure and the tool reports the stderr text.

## Options

| Argument | Values | Purpose |
|----------|--------|---------|
| `mode` | `full`, `selection`, `window` | Capture the full display or open macOS interactive selection UI |
| `label` | string | Adds a short slug to the filename |
| `display_id` | positive integer | Passes `-D <id>` to `screencapture` |
| `include_cursor` | boolean | Includes the mouse cursor |

`analyze_screenshot` accepts:

| Argument | Values | Purpose |
|----------|--------|---------|
| `image_path` | string | Container path returned by `take_screenshot`; omitted means latest screenshot |
| `prompt` | string | Question or instruction for the vision model |
| `model` | string | Optional LiteLLM/OpenAI-compatible model route |
| `max_output_tokens` | positive integer | Maximum analysis tokens, default `1200` |

`selection` and `window` are interactive host-desktop flows. They need an
active GUI session on the Mac.

## Container apps

This tool captures the Mac desktop, not an arbitrary container display. Pynchy
agent containers are normally headless. For browser pages, use the Playwright
browser tools. For other GUI apps inside a container, run the app under a display
server such as Xvfb/noVNC and capture that virtual display with a container-side
tool; Pynchy does not provide a generic container-GUI screenshot bridge.
