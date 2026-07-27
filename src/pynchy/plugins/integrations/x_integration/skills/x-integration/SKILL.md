---
name: x-integration
description: Post tweets, like, reply, retweet, and quote on X (Twitter) using browser automation. Use when the user asks you to interact with X/Twitter.
tier: social
---

# X (Twitter) Integration

Automate X/Twitter actions via Playwright browser automation, persistent Chromium profile.

## System requirements

Need system Chrome/Chromium binary — `CHROME_PATH` must be set in `.env` (e.g. `CHROME_PATH=/usr/bin/google-chrome-stable`). Playwright bundled Chromium not used — X fingerprint as bot.

All X tools run **headed mode** (not headless) — X detect + block headless browsers. Headless servers need Xvfb (`apt install xvfb`).

Interactive login via `setup_x_session` also need VNC (`apt install x11vnc novnc`).

## How it works

Playwright drive system Chrome binary (via `CHROME_PATH`) with persistent browser contexts. After one manual login (human do CAPTCHA / 2FA), next actions run auto from saved session. All tools use headed mode + anti-detection flags to dodge X bot fingerprinting.

## First-time setup (requires human)

Human must log in once before any X action:

```
setup_x_session()
```

Opens **visible** Chromium window at X login page. Human do login flow. Session saved for future auto use.

On **headless server** (no X display), tool auto-starts Xvfb + noVNC on port 6080. **Before call** `setup_x_session`, tell human open `http://<server>:6080/vnc.html?autoconnect=true` in browser.

**Hardware security keys (YubiKey, FIDO2):** noVNC cannot forward WebAuthn challenges — key must physically connect to machine running browser. If X login need hardware key, run `setup_x_session` on local machine with key attached, then rsync profile to server:

```bash
rsync -az data/playwright-profiles/x/ server:path/to/pynchy/data/playwright-profiles/x/
```

## Tools

### `setup_x_session(timeout_seconds=120)`

Launch headed browser for manual X login. Saves session to persistent profile for future use.

- `timeout_seconds` — wait time for login completion (default: 120s)

### `x_post(content)`

Post tweet. Content 1–280 chars.

### `x_like(tweet_url)`

Like tweet. Accepts full URL (`https://x.com/user/status/123`) or bare tweet ID.

### `x_reply(tweet_url, content)`

Reply to tweet. Content 1–280 chars.

### `x_retweet(tweet_url)`

Retweet, no comment.

### `x_quote(tweet_url, comment)`

Quote tweet with comment. Comment 1–280 chars.

## Error handling

- **"X login expired"** — Saved session expired. Human run `setup_x_session` again.
- **"Tweet not found"** — Tweet URL invalid or tweet deleted.
- **"Post/Submit button disabled"** — Content empty or over char limit.
- **"Login not completed within Xs"** — Human no finish manual login in time. Retry with bigger `timeout_seconds`.
- **"CHROME_PATH is required"** — Set `CHROME_PATH` in `.env` to system Chrome/Chromium binary path.
- **"No display available and Xvfb not installed"** — X tools need headed mode. Install Xvfb on server.
- **Selector errors** — X may change UI. Check data-testid selectors in plugin still match X React components.
