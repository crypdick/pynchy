---
name: slack-token-extractor
description: Refresh expired Slack browser tokens (xoxc/xoxd) using persistent browser sessions. Use when Slack MCP tools fail with authentication errors.
tier: ops
---

# Slack Token Extractor

Refreshes expired Slack browser session tokens (`xoxc`/`xoxd`) used by the Slack MCP server.

## System requirements

Playwright's Chromium browser and system libraries are auto-installed at startup (system libs require root; the script falls back gracefully if unprivileged).

On **headless servers**, `setup_slack_session` also needs a VNC stack for interactive login:

```bash
apt install xvfb x11vnc novnc
```

The script checks for missing VNC deps at startup and prints warnings.

## How it works

Uses Playwright persistent browser contexts — after one manual login (human handles CAPTCHA/magic-link), subsequent token extractions run headlessly using the saved session.

## First-time setup (requires human)

Before tokens can be refreshed automatically, a human must log in once:

```
setup_slack_session(workspace_name="acme")
```

This opens a **visible** Chromium window. The human completes the Slack login flow (CAPTCHA, magic link, SSO — whatever Slack requires). The session is saved for future headless use.

On a **headless server** (no X display), the tool automatically starts a virtual display with noVNC web access on port 6080. **Before calling** `setup_slack_session`, tell the human to open `http://<server>:6080/vnc.html?autoconnect=true` in their browser so they can interact with the login page.

**Hardware security keys (YubiKey, FIDO2):** noVNC cannot forward WebAuthn challenges — the key must be physically connected to the machine running the browser. If Slack login requires a hardware key, run `setup_slack_session` on a local machine with the key attached, then rsync the profile to the server:

```bash
rsync -az data/playwright-profiles/acme/ server:~/src/PERSONAL/pynchy/data/playwright-profiles/acme/
```

## Refreshing tokens

Once a session is set up, tokens can be refreshed headlessly:

```
refresh_slack_tokens(
    workspace_name="acme",
    xoxc_var="SLACK_XOXC_ACME",
    xoxd_var="SLACK_XOXD_ACME",
)
```

The tool navigates to Slack using the saved session, extracts fresh tokens, and writes them to `.env`. The pynchy service auto-restarts on `.env` changes.

## Error handling

- **"Not logged in — persistent session expired"** — The saved browser session has expired. A human needs to run `setup_slack_session` again.
- **"Failed to extract xoxc/xoxd"** — The browser reached the Slack client but tokens weren't found. Slack may have changed its storage format.
- **"Login not completed within Xs"** — The human didn't finish the manual login in time. Try again with a longer `timeout_seconds`.
