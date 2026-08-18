# Slack MCP Server

Set up [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) so agents can read Slack channels, messages, and user lists.

> **Freshness warning.** The upstream project may change its authentication flow or configuration at any time. If anything here doesn't match, check the [official authentication docs](https://github.com/korotovsky/slack-mcp-server/blob/master/docs/01-authentication-setup.md) first.

## Why this server?

Pynchy's built-in Slack channel plugin needs a bot token (`xoxb`), which means a workspace admin has to install a Slack app. This MCP server uses browser session tokens instead — works on any Slack workspace you can log into, even ones you don't admin. The tradeoff: browser tokens expire when you log out or Slack rotates sessions.

## Prerequisites

Read [MCP servers](../usage/mcp.md) for runtime configuration and
[Tool access and secrets](../usage/tool-access.md) for credential exposure
before continuing.

## 1. Define the server in `data/personalization/pynchy.toml`

Each Slack workspace gets its own tool entry with its own token mapping:

```toml
[tools.slack_mcp_acme]
type = "mcp"
required_env = [
  "SLACK_MCP_XOXC_TOKEN",
  "SLACK_MCP_XOXD_TOKEN",
]
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.slack_mcp_acme.mcp]
runtime = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8080
transport = "http"
env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8080" }
```

The upstream server reads these exact variable names. Pynchy passes them only
to the selected Docker tool process. `mcp.env` contains non-secret runtime
constants.

This upstream server's fixed names allow one browser-token pair in one Pynchy
host process. Multiple accounts require a provider wrapper that accepts
distinct environment names; declare those names in each tool's `required_env`.
Pynchy does not map one environment name to another.

## 2. Extract browser tokens

The Slack MCP server authenticates using browser session tokens (`xoxc` and `xoxd`). Not API tokens — they come from your logged-in Slack browser session.

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

Browser session tokens. They expire when you log out of Slack in the browser or when Slack rotates sessions. Once expired, the MCP server fails to authenticate and you need to extract fresh tokens.

## 3. Materialize the tokens

```
SLACK_MCP_XOXC_TOKEN=xoxc-...
SLACK_MCP_XOXD_TOKEN=xoxd-...
```

The variable names must match `required_env`. Use the ignored root `.env` for
local development. Use the Proton Pass host-process flow from
[Tool access and secrets](../usage/tool-access.md#materialize-host-secrets) for
production.

## 4. Grant workspace access

```toml
[profiles.acme-slack]
tools = ["slack_mcp_acme"]

[workspaces.acme-1]
profiles = ["acme-slack"]
```

The Docker container starts on-demand when an agent first needs it. Tools like `channels_list`, `channels_history`, and `users_list` become available to the agent.

## 5. Automated local token refresh (optional)

Instead of manually extracting tokens from DevTools every time they expire, use the **slack-token-extractor** plugin to automate extraction via a persistent browser session.

The approach: log into Slack once via a visible browser (handling CAPTCHA, magic links, SSO yourself). The browser session is saved. Future token extractions run headlessly against that saved session — no human interaction until Slack's full session expires.

### Setup

1. Grant the `slack_token_extractor` tool to a workspace (typically admin):

```toml
[tools.slack_token_extractor]
type = "builtin"
name = "slack_token_extractor"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = true

[profiles.slack-admin]
tools = ["slack_token_extractor"]

[workspaces.admin]
profiles = ["slack-admin"]
```

2. Install Playwright browsers on the host (one-time):

```bash
uv run --with playwright python -m playwright install chromium
```

### Initial login (one-time, requires display)

The first time, a human must complete the Slack login manually. Needs a display server on the host (VNC, SSH X-forwarding, or local desktop):

```
setup_slack_session(workspace_name="acme")
```

Opens a visible Chromium window at the Slack login page. Complete the login flow (CAPTCHA, magic link, SSO — whatever Slack requires). Once you reach the Slack client, the session is saved automatically.

On headless servers, the tool auto-starts a noVNC virtual display on port 6080 — open `http://<server>:6080/vnc.html?autoconnect=true` to interact with the browser. If Slack login requires a **hardware security key** (YubiKey/FIDO2), run `setup_slack_session` locally instead and rsync the profile:

```bash
rsync -az data/playwright-profiles/acme/ server:path/to/pynchy/data/playwright-profiles/acme/
```

### Refreshing tokens

Once the session is established, tokens refresh headlessly:

```
refresh_slack_tokens(
    workspace_name="acme",
    xoxc_var="SLACK_MCP_XOXC_TOKEN",
    xoxd_var="SLACK_MCP_XOXD_TOKEN",
)
```

The tool navigates to Slack using the saved session, extracts fresh tokens, and writes them to `.env`. The service auto-restarts on `.env` changes.
Use this flow only when `.env` owns the local credentials. For a production
Proton Pass deployment, update the corresponding Pass items through an
operator-authorized process instead.

### Scheduled refresh

For unattended operation, configure a periodic workspace that calls the tool on a schedule:

```toml
# data/personalization/pynchy.toml
[profiles.token-refresh]
tools = ["slack_token_extractor"]

[workspaces.token-refresh]
profiles = ["token-refresh"]
```

```toml
# data/personalization/automations/refresh-slack-tokens/config.toml
schema_version = 1

[job]
enabled = true
schedule = "0 4 * * 1"  # weekly, Monday 4am
workspace = "token-refresh"
prompt = "Refresh Slack tokens for ACME workspace using the slack_token_extractor tool."
```

When the persistent session expires (Slack rotates sessions periodically), the scheduled refresh fails with "Not logged in". Run `setup_slack_session` again to re-establish the session.

## 6. Verify

After the service restarts, trigger a message in the workspace. The Slack MCP Docker container should start on-demand. Check with:

```bash
ssh your-server 'docker ps --filter name=pynchy-mcp-slack'
ssh your-server 'journalctl --user -u pynchy --grep "MCP container ready" -n 5'
```
