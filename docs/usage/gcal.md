# Google Calendar

Gives agents access to your Google Calendar. Agents can list calendars, read events, create events, and manage calendar entries.

## Prerequisites

Same as [Google Drive](gdrive.md#prerequisites) — system Chrome and virtual display packages on the host.

## 1. Define chrome profile and tool

Add a chrome profile and Google Calendar tool to `config.toml`:

```toml
chrome_profiles = ["mycompany"]

[tools."gcal.mycompany"]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools."gcal.mycompany".mcp]
runtime = "docker"
image = "pynchy-mcp-gcal:latest"
dockerfile = "src/pynchy/agent/mcp/gcal.Dockerfile"
port = 3200
transport = "streamable_http"
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]
```

## 2. Grant workspace access

```toml
[profiles.google-mycompany]
tools = ["gdrive.mycompany", "gcal.mycompany"]

[workspaces.mycompany-1]
profiles = ["google-mycompany"]
```

## 3. First-time setup

Ask your agent to set up Google for the profile:

```
@Pynchy set up Google for the mycompany profile
```

The agent calls `setup_google(chrome_profile="mycompany")`. Idempotent — checks what's already done and only runs the missing steps. Required OAuth scopes are auto-computed from which services (gdrive, gcal) reference the profile.

On a **headless server**, the agent returns a noVNC URL for browser interaction.

## 4. Verify

```bash
ssh your-server 'docker ps --filter name=pynchy-mcp-gcal'
```

## Multiple accounts

Each chrome profile maps to one Google account. To access calendars from multiple accounts:

```toml
chrome_profiles = ["mycompany", "personal"]

[tools."gcal.mycompany"]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools."gcal.mycompany".mcp]
runtime = "docker"
image = "pynchy-mcp-gcal:latest"
dockerfile = "src/pynchy/agent/mcp/gcal.Dockerfile"
port = 3200
transport = "streamable_http"
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]

[tools."gcal.personal"]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools."gcal.personal".mcp]
runtime = "docker"
image = "pynchy-mcp-gcal:latest"
dockerfile = "src/pynchy/agent/mcp/gcal.Dockerfile"
port = 3201
transport = "streamable_http"
volumes = ["data/chrome-profiles/personal:/home/chrome"]

[profiles.google-calendars]
tools = ["gcal.mycompany", "gcal.personal"]

[workspaces.mycompany-1]
profiles = ["google-calendars"]
```

The agent sees separate tool namespaces: `mcp__gcal_mycompany__list_events` and `mcp__gcal_personal__list_events`.

## How it works

The gcal MCP server uses `@cocal/google-calendar-mcp`, which has native Streamable HTTP support (no supergateway needed). Credentials from the chrome profile directory mount into the container at `/home/chrome/`. The entrypoint copies tokens to gcal's expected format.
