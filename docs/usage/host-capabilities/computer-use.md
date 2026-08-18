# Computer Use

`computer_use` lets agents inspect and operate a host desktop when browser
automation is the wrong shape: native apps, sensitive logged-in sites, and
permission or login flows that need the real local desktop.

The agent container never receives raw desktop access. A backend-neutral service
accepts the request through Pynchy's policy-enforced IPC boundary, preserves
workspace attribution, and sends it to one explicitly selected provider plugin
on the host. This keeps computer use optional and replaceable without silently
changing automation implementations.

## Select a provider

Select exactly one provider:

```toml
[plugins.computer-use.options]
provider = "peekaboo"
```

Pynchy reports an unavailable selected provider instead of choosing another
implementation. Fix the provider or change this setting explicitly. Pynchy
never retries a failed action through another provider because doing so could
repeat a partially completed click, keystroke, or other mutation.

Peekaboo and Cua Driver require macOS. Pynchy also includes an SSH X11
provider for controlling an existing Linux desktop session without relaunching
its applications or browser profile.

```toml
[plugins.peekaboo]
enabled = false

[plugins.cua-driver]
enabled = false

[plugins.computer-use.options]
provider = "my-linux-provider"
```

## Built-in: SSH X11

`ssh-x11` controls a real X11 desktop through a pinned SSH credential and the
packaged `pynchy-x11-computer-use` helper. The remote machine needs `wmctrl`,
ImageMagick `import`, and `xdotool`. Install the same Pynchy release on that
machine so the helper command stays versioned with the host.

Restrict a dedicated SSH public key to the helper in the remote account's
`authorized_keys`. Replace the placeholders with the installed helper's
absolute path and public key:

```text
restrict,command="<helper-path>/pynchy-x11-computer-use" ssh-ed25519 <public-key>
```

Then configure the endpoint:

```toml
[plugins.computer-use.options]
provider = "ssh-x11"

[plugins.ssh-x11.options]
host = "100.64.0.10"
user = "desktop-user"
private_key = "/run/secrets/pynchy-x11/id_ed25519"
known_hosts = "/run/secrets/pynchy-x11/known_hosts"
timeout_seconds = 30
```

The host requests no remote command; OpenSSH's forced-command policy chooses the
helper. Pynchy checks helper protocol version, desktop binaries, and active X11
session through a read-only handshake. Pin the host key and restrict reachability
with SSH server and tailnet policy. Supported actions include capture,
app/window listing, coordinate clicks, text, shortcuts, scrolling, and permission
checks. Captures focus a selected window but return the complete real desktop so
X11 compositors cannot substitute a blank per-window image.

## Built-in: Peekaboo

[Peekaboo](https://github.com/steipete/Peekaboo) is the preferred macOS
provider. It offers semantic accessibility snapshots, stable element
references, application and window targeting, menus, dialogs, clipboard
operations, and Spaces management.

Install it on the macOS host and inspect its permissions:

```bash
brew install steipete/tap/peekaboo
peekaboo permissions status
```

Grant Screen Recording and Accessibility in macOS System Settings. Coordinate
clicks, keyboard input, and synthetic click fallback can also require Event
Synthesizing permission.

Override its executable or timeout when necessary:

```toml
[plugins.peekaboo.options]
binary = "/opt/homebrew/bin/peekaboo"
timeout_seconds = 30
```

Start by listing applications, then target an application or PID when listing
windows:

```text
computer_use(action="list_apps")
computer_use(action="list_windows", app="TextEdit")
computer_use(action="capture", app="TextEdit", label="editor")
```

A capture returns a semantic snapshot ID, stable element references such as
`B1` or `T2`, and a PNG artifact. Reuse the snapshot ID and element reference
for the next action:

```text
computer_use(action="click", app="TextEdit", snapshot_id="SNAPSHOT", element="B1")
computer_use(action="set_value", snapshot_id="SNAPSHOT", element="T2", value="hello")
```

The provider also supports:

- clicks, text, shortcuts, scrolling, accessibility actions, and value changes
- application launch and application/window discovery
- menu discovery and selection
- dialog discovery, buttons, text input, file selection, and dismissal
- clipboard get, set, clear, save, and restore
- Space discovery, switching, and moving windows between Spaces

## Built-in: Cua Driver

[Cua Driver](https://cua.ai/cua-driver) remains available as a compatibility
provider. Install it on the macOS host and verify its daemon and permissions:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
cua-driver call check_permissions
cua-driver status
```

```toml
[plugins.cua-driver.options]
binary = "cua-driver"
timeout_seconds = 30
```

Cua Driver supports captures, application/window discovery, application
launch, numeric-element or coordinate clicks, text, shortcuts, scrolling, and
permission checks. Stable element references, menus, dialogs, clipboard, and
Spaces require a richer provider such as Peekaboo.

## Compose unrestricted computer use into profiles

Define one reusable profile when multiple workspaces should operate the host
desktop without runtime approval:

```toml
[tools.computer_use]
type = "builtin"
name = "computer_use"

[profiles.unrestricted-computer-use]
tools = ["computer_use"]

[profiles.unrestricted-computer-use.capabilities."desktop.computer.use"]
decision = "allow"

[profiles.desktop-worker]
includes = ["base", "unrestricted-computer-use"]
```

Register the builtin tool once, then reuse the profile wherever needed. The
tool selection makes `computer_use` available. The explicit capability rule
authorizes it without the normal session prompt. The built-in computer-use
plugin supplies its core instructional skill automatically, so the profile
does not need a separate `skills` entry.

Compose this profile only into workspaces intended to control the real host
without confirmation. A service property set to `"forbidden"` still blocks
the action. See [Capability Rules](../security.md#capability-rules) for the full
policy precedence.

## Artifacts and safety

Screenshots are saved under the workspace IPC directory and exposed inside the
agent container at `/run/pynchy/computer-use/<file>.png`.

This tool controls the real host desktop. Gate it with workspace security
policy for non-admin workspaces. When policy requires approval, the approval
covers only `computer_use` for the active agent session and clears when that
session ends. Other tools keep their own approval scope, and no approval
overrides a later policy denial.
Agents should not enter secrets, payment details, 2FA codes, or destructive
confirmations unless the user explicitly authorized that exact action.

---

**Want to customize this?** Write your own provider plugin using the
[`pynchy_computer_use_backend` hook](../../plugins/hooks/host-services.md#pynchy_computer_use_backend),
or [open a feature request](https://github.com/crypdick/pynchy/issues).
