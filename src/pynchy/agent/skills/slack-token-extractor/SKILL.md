---
name: slack-token-extractor
description: Refresh expired Slack browser tokens (xoxc/xoxd) using persistent browser sessions. Use when Slack MCP tools fail with authentication errors.
tier: ops
---

# Slack Token Extractor

Refresh expired Slack browser session tokens (`xoxc`/`xoxd`) used by Slack MCP server.

## System requirements

Need system Chrome/Chromium binary — `CHROME_PATH` must set in `.env` (e.g. `CHROME_PATH=/usr/bin/google-chrome-stable`). Playwright bundled Chromium never used — services fingerprint as bot traffic.

On **headless servers**, `setup_slack_session` also need VNC stack for interactive login:

```bash
apt install xvfb x11vnc novnc
```

Plugin check missing deps at startup, log warnings.

## How it works

Use Playwright persistent browser contexts — after one manual login (human handle CAPTCHA/magic-link), later token extractions run headless using saved session.

## First-time setup (requires human)

Before tokens auto-refresh, human must log in once:

```
setup_slack_session(workspace_name="acme")
```

Open **visible** Chromium window. Human complete Slack login flow (CAPTCHA, magic link, SSO — whatever Slack require). Session saved for future headless use.

On **headless server** (no X display), tool auto-start virtual display with noVNC web access on port 6080. **Before calling** `setup_slack_session`, tell human open `http://<server>:6080/vnc.html?autoconnect=true` in browser so can interact with login page.

**Hardware security keys (YubiKey, FIDO2):** noVNC cannot forward WebAuthn challenges — key must physically connect to machine running browser. If Slack login need hardware key, run `setup_slack_session` on local machine with key attached, then rsync profile to server:

```bash
rsync -az data/playwright-profiles/acme/ server:/path/to/pynchy/data/playwright-profiles/acme/
```

## Refreshing tokens

Once session set up, tokens refresh headless:

```
refresh_slack_tokens(
    workspace_name="acme",
    xoxc_var="SLACK_XOXC_ACME",
    xoxd_var="SLACK_XOXD_ACME",
)
```

Tool navigate to Slack using saved session, extract fresh tokens, write to `.env`. pynchy service auto-restart on `.env` changes.

## Error handling

- **"Not logged in — persistent session expired"** — Saved browser session expired. Human need run `setup_slack_session` again.
- **"Failed to extract xoxc/xoxd"** — Browser reached Slack client but tokens not found. Slack may have changed storage format.
- **"Login not completed within Xs"** — Human not finish manual login in time. Try again with longer `timeout_seconds`.
