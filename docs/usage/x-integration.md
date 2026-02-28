# X (Twitter) Integration

Automates X/Twitter actions (post, like, reply, retweet, quote) via browser automation. Uses Playwright to drive a real browser with a persistent login session — no X API subscription required.

## Why browser automation?

X's official API requires a paid subscription ($100+/month) for posting. Browser automation uses your existing X session instead. The tradeoff is that it depends on X's UI selectors (which can change) and requires a headed browser (no headless mode — X actively detects it).

## Prerequisites

On the host machine (pynchy-server):

```bash
# System Chrome — required (Playwright's bundled Chromium is not used because
# X fingerprints it as a bot)
apt install google-chrome-stable

# Virtual display — required on headless servers (X tools use headed mode)
apt install xvfb

# VNC — required for setup_x_session interactive login
apt install x11vnc novnc
```

Add `CHROME_PATH` to `.env`:

```
CHROME_PATH=/usr/bin/google-chrome-stable
```

The script will refuse to start without `CHROME_PATH` — Playwright only provides the automation protocol (CDP), while Chrome provides the browser with a genuine fingerprint that X won't flag.

## 1. Enable the plugin

The X integration plugin is built-in but disabled by default. Enable it in `config.toml`:

```toml
[plugins.x-integration]
enabled = true
```

## 2. Grant workspace access

Add the `x_integration` MCP server to a workspace:

```toml
[workspaces.admin]
mcp_servers = ["x_integration"]
```

## 3. First-time authentication

A human must log in to X once via a visible browser. The agent calls `setup_x_session()`, which opens Chromium at the X login page. The human completes the login flow (CAPTCHA, 2FA, etc.), and the session is saved for future automated use.

On a **headless server**, the tool automatically starts a virtual display with noVNC on port 6080. Before triggering the tool, open `http://<server>:6080/vnc.html?autoconnect=true` in your browser to see and interact with the login page.

### Hardware security keys

noVNC cannot forward WebAuthn (YubiKey/FIDO2) challenges. If your X login requires a hardware key:

1. Run `setup_x_session` on a local machine with the key attached
2. Copy the profile to the server:

```bash
rsync -az data/playwright-profiles/x/ pynchy-server:path/to/pynchy/data/playwright-profiles/x/
```

## 4. Using X tools

Once authenticated, the agent can use these tools:

| Tool | Description |
|------|-------------|
| `x_post(content)` | Post a tweet (max 280 chars) |
| `x_like(tweet_url)` | Like a tweet |
| `x_reply(tweet_url, content)` | Reply to a tweet |
| `x_retweet(tweet_url)` | Retweet without comment |
| `x_quote(tweet_url, comment)` | Quote tweet with comment |

All tools accept full URLs (`https://x.com/user/status/123`) or bare tweet IDs.

## Troubleshooting

### Session expired

If tools return "X login expired", the browser session has expired. Run `setup_x_session` again — a human needs to complete the login.

### Selector errors

X may update their UI, breaking the `data-testid` selectors the script relies on. Check the selector table in [`src/pynchy/agent/skills/x-integration/SKILL.md`](https://github.com/crypdick/pynchy/blob/main/src/pynchy/agent/skills/x-integration/SKILL.md) and compare against X's current DOM.

### Browser lock files

If the browser fails to launch after a crash:

```bash
rm -f data/playwright-profiles/x/SingletonLock
rm -f data/playwright-profiles/x/SingletonSocket
rm -f data/playwright-profiles/x/SingletonCookie
```

### No display errors

X tools require headed mode. On headless servers, ensure Xvfb is installed (`apt install xvfb`). The script starts it automatically.

### CHROME_PATH errors

The script requires `CHROME_PATH` in `.env`. If you see "CHROME_PATH is required" or "does not exist", install Chrome and set the path:

```bash
apt install google-chrome-stable
# Add to .env:
CHROME_PATH=/usr/bin/google-chrome-stable
```
