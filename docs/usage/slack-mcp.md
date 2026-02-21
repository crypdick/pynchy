# Slack MCP Server

This page covers setting up [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) so your agents can read Slack channels, messages, and user lists.

> **Freshness warning.** The upstream project may change its authentication flow or configuration at any time. If anything here doesn't match, check the [official authentication docs](https://github.com/korotovsky/slack-mcp-server/blob/master/docs/01-authentication-setup.md) first.

## Why this server?

Pynchy's built-in Slack channel plugin requires a bot token (`xoxb`), which means a workspace admin has to install a Slack app. This MCP server uses browser session tokens instead — you can connect to any Slack workspace you can log into, even ones you don't admin. The tradeoff is that browser tokens expire when you log out or Slack rotates sessions.

## Prerequisites

You should already understand how pynchy manages MCP servers. If not, read the [MCP servers guide](mcp.md) first — especially the sections on `env_forward` and multi-tenant setup.

## 1. Define the server in `config.toml`

Each Slack workspace gets its own server entry with its own token mapping:

```toml
[mcp_servers.slack_mcp_acme]
type = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8080
transport = "http"
env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8080" }
env_forward = { SLACK_MCP_XOXC_TOKEN = "SLACK_XOXC_ACME", SLACK_MCP_XOXD_TOKEN = "SLACK_XOXD_ACME" }
```

The `env_forward` mapping means: the Docker container sees `SLACK_MCP_XOXC_TOKEN`, resolved from `SLACK_XOXC_ACME` in the host `.env`.

For multiple Slack workspaces, add another entry with a different name, port, and `env_forward` mapping. See [MCP servers § Multi-tenant servers](mcp.md#multi-tenant-servers) for the pattern.

## 2. Extract browser tokens

The Slack MCP server authenticates using browser session tokens (`xoxc` and `xoxd`). These are not API tokens — they come from your logged-in Slack browser session.

### Get the `xoxc` token

1. Open Chrome and navigate to your Slack workspace (e.g., `https://app.slack.com`)
2. Open DevTools (`F12` or `Ctrl+Shift+I`)
3. Go to the **Console** tab
4. Try to paste the snippet below — Chrome will block the paste and show a warning
5. Type `allow pasting` in the console and press Enter
6. Now paste and execute:
   ```js
   JSON.parse(localStorage.localConfig_v2).teams[document.location.pathname.match(/^\/client\/([A-Z0-9]+)/)[1]].token
   ```
7. Copy the resulting `xoxc-...` value

### Get the `xoxd` token

1. In the same DevTools, go to **Application** → **Cookies** (left sidebar)
2. Click on `https://app.slack.com`
3. Find the cookie named `d` (literally the single letter)
4. Copy its value — it starts with `xoxd-...`

### Token lifetime

These are browser session tokens. They expire when you log out of Slack in the browser or when Slack rotates sessions. When they expire, the MCP server will fail to authenticate and you'll need to extract fresh tokens.

## 3. Add tokens to `.env`

```
SLACK_XOXC_ACME=xoxc-...
SLACK_XOXD_ACME=xoxd-...
```

The variable names must match the right-hand side of your `env_forward` mapping from step 1. Changes to `.env` trigger an automatic service restart.

## 4. Grant workspace access

```toml
[workspaces.acme-1]
mcp_servers = ["slack_mcp_acme"]
```

The Docker container starts on-demand when an agent first needs it. Tools like `channels_list`, `channels_history`, and `users_list` become available to the agent.

## 5. Automated token refresh (optional)

Instead of manually extracting tokens from DevTools every time they expire, you can use the **slack-token-extractor** plugin to automate extraction via a persistent browser session.

The approach: you log into Slack once via a visible browser (handling CAPTCHA, magic links, SSO yourself). The browser session is saved. Future token extractions run headlessly using that saved session — no human interaction needed until Slack's full session expires.

### Setup

1. Grant the `slack_token_extractor` MCP server to a workspace (typically admin):

```toml
[workspaces.admin]
mcp_servers = ["slack_token_extractor"]
```

2. Install Playwright browsers on the host (one-time):

```bash
uv run --with playwright python -m playwright install chromium
```

### Initial login (one-time, requires display)

The first time, a human must complete the Slack login manually. This requires a display server on the host (VNC, SSH X-forwarding, or local desktop):

```
setup_slack_session(workspace_name="acme")
```

This opens a visible Chromium window at the Slack login page. Complete the login flow (CAPTCHA, magic link, SSO — whatever Slack requires). Once you reach the Slack client, the session is saved automatically.

### Refreshing tokens

Once the session is established, tokens can be refreshed headlessly:

```
refresh_slack_tokens(
    workspace_name="acme",
    xoxc_var="SLACK_XOXC_ACME",
    xoxd_var="SLACK_XOXD_ACME",
)
```

The tool navigates to Slack using the saved session, extracts fresh tokens, and writes them to `.env`. The service auto-restarts on `.env` changes.

### Scheduled refresh

For unattended operation, configure a periodic workspace that calls the tool on a schedule:

```toml
[workspaces.token-refresh]
is_periodic = true
schedule = "0 4 * * 1"  # weekly, Monday 4am
prompt = "Refresh Slack tokens for ACME workspace using the slack_token_extractor tool."
mcp_servers = ["slack_token_extractor"]
```

When the persistent session expires (Slack rotates sessions periodically), the scheduled refresh will fail with "Not logged in". Run `setup_slack_session` again to re-establish the session.

## 6. Verify

After the service restarts, trigger a message in the workspace. The Slack MCP Docker container should start on-demand. Check with:

```bash
ssh pynchy-server 'docker ps --filter name=pynchy-mcp-slack'
ssh pynchy-server 'journalctl --user -u pynchy --grep "MCP container ready" -n 5'
```
