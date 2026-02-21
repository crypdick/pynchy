---
name: x-integration
description: Post tweets, like, reply, retweet, and quote on X (Twitter) using browser automation. Use when the user asks you to interact with X/Twitter.
tier: ops
---

# X (Twitter) Integration

Automates X/Twitter actions via Playwright browser automation with a persistent Chromium profile.

## System requirements

Requires a system Chrome/Chromium binary — `CHROME_PATH` must be set in `.env` (e.g. `CHROME_PATH=/usr/bin/google-chrome-stable`). Playwright's bundled Chromium is not used because X fingerprints it as a bot.

All X tools run in **headed mode** (not headless) because X actively detects and blocks headless browsers. On headless servers, this requires Xvfb (`apt install xvfb`).

For interactive login via `setup_x_session`, you also need VNC (`apt install x11vnc novnc`).

## How it works

Playwright drives the system Chrome binary (via `CHROME_PATH`) using persistent browser contexts. After one manual login (human handles CAPTCHA / 2FA), subsequent actions run automatically using the saved session. All tools use headed mode with anti-detection flags to avoid X's bot fingerprinting.

## First-time setup (requires human)

Before any X actions can be performed, a human must log in once:

```
setup_x_session()
```

This opens a **visible** Chromium window at the X login page. The human completes the login flow. The session is saved for future automated use.

On a **headless server** (no X display), the tool automatically starts Xvfb + noVNC on port 6080. **Before calling** `setup_x_session`, tell the human to open `http://<server>:6080/vnc.html?autoconnect=true` in their browser.

**Hardware security keys (YubiKey, FIDO2):** noVNC cannot forward WebAuthn challenges — the key must be physically connected to the machine running the browser. If X login requires a hardware key, run `setup_x_session` on a local machine with the key attached, then rsync the profile to the server:

```bash
rsync -az data/playwright-profiles/x/ server:path/to/pynchy/data/playwright-profiles/x/
```

## Tools

### `setup_x_session(timeout_seconds=120)`

Launch a headed browser for manual X login. Saves the session to a persistent profile for future use.

- `timeout_seconds` — how long to wait for login completion (default: 120s)

### `x_post(content)`

Post a tweet. Content must be 1–280 characters.

### `x_like(tweet_url)`

Like a tweet. Accepts a full URL (`https://x.com/user/status/123`) or a bare tweet ID.

### `x_reply(tweet_url, content)`

Reply to a tweet. Content must be 1–280 characters.

### `x_retweet(tweet_url)`

Retweet without comment.

### `x_quote(tweet_url, comment)`

Quote tweet with a comment. Comment must be 1–280 characters.

## Error handling

- **"X login expired"** — The saved browser session has expired. A human needs to run `setup_x_session` again.
- **"Tweet not found"** — The tweet URL is invalid or the tweet was deleted.
- **"Post/Submit button disabled"** — Content may be empty or exceed the character limit.
- **"Login not completed within Xs"** — The human didn't finish the manual login in time. Try again with a longer `timeout_seconds`.
- **"CHROME_PATH is required"** — Set `CHROME_PATH` in `.env` to the system Chrome/Chromium binary path.
- **"No display available and Xvfb not installed"** — X tools need headed mode. Install Xvfb on the server.
- **Selector errors** — X may have changed its UI. Check if the data-testid selectors in the plugin still match X's React components.
