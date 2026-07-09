# Computer Use

`computer_use` lets agents drive the macOS host desktop through
[Cua Driver](https://cua.ai/cua-driver). It is for workflows where browser/CDP
automation is the wrong shape: sensitive logged-in sites, anti-bot-prone
consumer sites, native apps, and permission/login flows that need a real local
desktop.

The agent container does not get raw desktop access. It sends an IPC service
request to Pynchy; the host-side plugin runs `cua-driver call ...` and returns
structured output plus screenshot artifacts.

## Requirements

Install Cua Driver on the macOS host:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
```

Grant `CuaDriver.app` Accessibility and Screen Recording in macOS System
Settings. Cua's daemon normally starts on demand, but you can verify the install
with:

```bash
cua-driver call check_permissions
cua-driver status
```

## Workflow

Launch or locate an app:

```text
computer_use(action="launch_app", bundle_id="com.google.Chrome", urls=["https://example.com"])
computer_use(action="list_windows")
```

Capture a target window:

```text
computer_use(action="capture", pid=844, window_id=10725, label="chrome")
```

Act and verify:

```text
computer_use(action="click", pid=844, window_id=10725, element=14, capture_after=true)
computer_use(action="type", pid=844, text="hello")
computer_use(action="key", pid=844, keys="cmd+s")
```

Screenshots are saved under the workspace IPC directory and are readable inside
the agent container at `/workspace/ipc/computer-use/<file>.png`.

## Safety

This tool controls the real host desktop. Use the workspace security policy to
gate it for non-admin workspaces. Agents should not enter secrets, payment
details, 2FA codes, or destructive confirmations unless the user explicitly
authorized that exact action.
