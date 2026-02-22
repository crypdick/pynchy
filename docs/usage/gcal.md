# Google Calendar

Gives agents access to your Google Calendar. Agents can list calendars, read events, create events, and manage calendar entries.

## Prerequisites

Same as [Google Drive](gdrive.md#prerequisites) — system Chrome and virtual display packages on the host.

## 1. Define chrome profile and instance

Add a chrome profile and gcal instance to `config.toml`:

```toml
chrome_profiles = ["anyscale"]

[mcp_servers.gcal.anyscale]
chrome_profile = "anyscale"
```

The plugin provides the base spec (Docker image, port, transport). You only declare the instance with its chrome profile attachment.

## 2. Grant workspace access

```toml
[workspaces.anyscale-1]
mcp_servers = ["gcal.anyscale"]
```

Use `mcp_groups` for convenience when combining multiple Google services:

```toml
[mcp_groups]
google_anyscale = ["gdrive.anyscale", "gcal.anyscale"]

[workspaces.anyscale-1]
mcp_servers = ["google_anyscale"]
```

## 3. First-time setup

Ask your agent to set up Google for the profile:

```
@Pynchy set up Google for the anyscale profile
```

The agent calls `setup_google(chrome_profile="anyscale")`. This is idempotent — it checks what's already done and only runs the missing steps. Required OAuth scopes are auto-computed from which services (gdrive, gcal) reference the profile.

On a **headless server**, the agent returns a noVNC URL for browser interaction.

## 4. Verify

```bash
ssh pynchy-server 'docker ps --filter name=pynchy-mcp-gcal'
```

## Multiple accounts

Each chrome profile maps to one Google account. To access calendars from multiple accounts:

```toml
chrome_profiles = ["anyscale", "personal"]

[mcp_servers.gcal.anyscale]
chrome_profile = "anyscale"

[mcp_servers.gcal.personal]
chrome_profile = "personal"

[workspaces.anyscale-1]
mcp_servers = ["gcal.anyscale", "gcal.personal"]
```

The agent sees separate tool namespaces: `mcp__gcal_anyscale__list_events` and `mcp__gcal_personal__list_events`.

## How it works

The gcal MCP server uses `@cocal/google-calendar-mcp`, which has native Streamable HTTP support (no supergateway needed). Credentials from the chrome profile directory are mounted into the container at `/home/chrome/`. The entrypoint copies tokens to gcal's expected format.
