# Google Drive

Give agents read-only access to Drive files so they can search, list, and read
documents, spreadsheets, and other files. Complete the shared [Google
setup](index.md) first.

## Configure the Drive tool

Add a Chrome profile and Drive tool to `data/personalization/pynchy.toml`:

```toml
chrome_profiles = ["mycompany"]

[tools."gdrive.mycompany"]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools."gdrive.mycompany".mcp]
runtime = "docker"
image = "pynchy-mcp-gdrive:latest"
dockerfile = "src/pynchy/agent/mcp/gdrive.Dockerfile"
build_context = "src/pynchy/agent/mcp"
port = 3100
transport = "streamable_http"
env = { GDRIVE_OAUTH_PATH = "/home/chrome/gcp-oauth.keys.json" }
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]

[profiles.mycompany-google]
tools = ["gdrive.mycompany"]

[workspaces.mycompany-1]
profiles = ["mycompany-google"]
```

## Verify

Send a request in a workspace with `gdrive.mycompany` access. The server starts
on demand. On a remote host, verify its container:

```bash
ssh your-server 'docker ps --filter name=pynchy-mcp-gdrive'
```

## Troubleshooting

### 403 errors from Drive API

The Drive API is not enabled for the Google Cloud project. Re-run the shared
Google setup; it detects and enables missing services.

### Token expired or authentication errors

Re-run the shared Google setup. It detects existing credentials and performs
only the required OAuth steps.

### noVNC does not load

Install `xvfb`, `x11vnc`, and `novnc` on the host. The setup tool starts the
virtual display, but it cannot install those packages.

### Browser lock files after a crash

```bash
rm -f data/playwright-profiles/google/SingletonLock
rm -f data/playwright-profiles/google/SingletonSocket
rm -f data/playwright-profiles/google/SingletonCookie
```

## Operational canary

Drive authorization uses a read-only scope, so its operational check never
creates or edits a file. Keep one harmless fixture in Drive and add its ID plus
a restrictive search query to `[canary]`:

```toml
scenario_ids = ["drive.google.round.trip"]
google_drive_server = "gdrive.mycompany"
google_drive_probe_query = "pynchy-canary-fixture"
google_drive_file_id = "your-fixture-file-id"
```

The canary opens a real MCP session, verifies the expected tools, searches for
the fixture, and reads it in a fresh session. It records redacted evidence only.

## Migrate an earlier Drive setup

If an earlier setup used a Docker named volume, create a Chrome profile and move
the OAuth client JSON into it:

```bash
mkdir -p data/chrome-profiles/mycompany
cp data/gcp-oauth.keys.json data/chrome-profiles/mycompany/gcp-oauth.keys.json
```

Then configure `gdrive.mycompany`, select it through a profile, and re-run the
shared Google setup. Tokens in the old Docker volume do not carry over. After a
successful authorization, remove the old volume and JSON file.
