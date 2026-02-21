# X (Twitter) Integration

Automates X/Twitter actions (post, like, reply, retweet, quote) via browser automation. Uses Playwright to drive a real browser with a persistent login session — no X API subscription required.

## Why browser automation?

X's official API requires a paid subscription ($100+/month) for posting. Browser automation uses your existing X session instead. The tradeoff is that it depends on X's UI selectors (which can change) and requires a headed browser (no headless mode — X actively detects it).

## Prerequisites

On the host machine (pynchy-server):

```bash
# Required for all X tools (headed mode needs a virtual display)
apt install xvfb

# Required for setup_x_session interactive login via VNC
apt install xvfb x11vnc novnc
```

**Recommended:** Install system Chrome for a genuine browser fingerprint. Playwright's bundled Chromium works but may be fingerprinted by X:

```bash
# Debian/Ubuntu
apt install google-chrome-stable
# Then add to .env:
CHROME_PATH=/usr/bin/google-chrome-stable
```

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

X may update their UI, breaking the `data-testid` selectors the script relies on. Check the selector table in [`container/skills/x-integration/SKILL.md`](https://github.com/crypdick/pynchy/blob/main/container/skills/x-integration/SKILL.md) and compare against X's current DOM.

### Browser lock files

If the browser fails to launch after a crash:

```bash
rm -f data/playwright-profiles/x/SingletonLock
rm -f data/playwright-profiles/x/SingletonSocket
rm -f data/playwright-profiles/x/SingletonCookie
```

### No display errors

X tools require headed mode. On headless servers, ensure Xvfb is installed (`apt install xvfb`). The script starts it automatically.

### Bot detection

If X starts blocking actions, switch to system Chrome (set `CHROME_PATH` in `.env`). Playwright's bundled Chromium has a different fingerprint that X may flag.
