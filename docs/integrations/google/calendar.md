# Google Calendar

Give agents access to a Google Calendar so they can list calendars, read events,
create events, and manage calendar entries. Complete the shared [Google
setup](index.md) first.

## Configure the Calendar tool

Add a Chrome profile and Calendar tool to
`data/personalization/pynchy.toml`:

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
build_context = "src/pynchy/agent/mcp"
port = 3200
transport = "streamable_http"
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]

[profiles.google-mycompany]
tools = ["gcal.mycompany"]

[workspaces.mycompany-1]
profiles = ["google-mycompany"]
```

## Verify

Send a request in a workspace with `gcal.mycompany` access. The server starts
on demand. On a remote host, verify its container:

```bash
ssh your-server 'docker ps --filter name=pynchy-mcp-gcal'
```

## How it works

The server uses `@cocal/google-calendar-mcp` with native Streamable HTTP
support. Pynchy mounts the selected Chrome profile at `/home/chrome`; the
entrypoint copies OAuth tokens into the format Calendar expects.

## Operational canary

To observe an authorized Calendar integration, create a dedicated calendar and
select its managed server in `[canary]`:

```toml
scenario_ids = ["calendar.google.round.trip"]
google_calendar_server = "gcal.mycompany"
google_calendar_id = "pynchy-canary"
```

The check lists capabilities, creates a tagged event, retrieves it in a fresh
session, deletes it with `sendUpdates = "none"`, and confirms it is gone. Do
not point it at a personal or shared calendar.
