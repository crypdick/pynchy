# Google Drive

Gives agents read-only access to your Google Drive files. Agents can search, list, and read documents, spreadsheets, and other Drive content.

## Prerequisites

On the Pynchy host:

```bash
# System Chrome — required for GCP Console automation during setup
apt install google-chrome-stable

# Virtual display — required on headless servers for interactive OAuth consent
apt install xvfb x11vnc novnc
```

Add `CHROME_PATH` to `.env`:

```
CHROME_PATH=/usr/bin/google-chrome-stable
```

## 1. Define chrome profile and tool

Add a chrome profile and Google Drive tool to `config.toml`:

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
port = 3100
transport = "streamable_http"
env = { GDRIVE_OAUTH_PATH = "/home/chrome/gcp-oauth.keys.json" }
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]
```

## 2. Grant workspace access

```toml
[profiles.mycompany-google]
tools = ["gdrive.mycompany"]

[workspaces.mycompany-1]
profiles = ["mycompany-google"]
```

## 3. First-time setup

Ask your agent to set up Google for the profile:

```
@Pynchy set up Google for the mycompany profile
```

The agent calls `setup_google(chrome_profile="mycompany")`, which automates the full GCP setup: create a project, enable the Drive API, configure OAuth consent, create credentials, run the OAuth authorization. You click "Allow" on the Google consent screen to grant read-only Drive access.

On a **headless server**, the agent returns a noVNC URL — open it to interact with the GCP Console and Google login.

## 4. Verify

Trigger a message in a workspace with `gdrive.mycompany` access. The Docker container starts on-demand:

```bash
ssh your-server 'docker ps --filter name=pynchy-mcp-gdrive'
```

## Multiple accounts

Each chrome profile maps to one Google account. To access Drive from multiple accounts:

```toml
chrome_profiles = ["mycompany", "work"]

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
port = 3100
transport = "streamable_http"
env = { GDRIVE_OAUTH_PATH = "/home/chrome/gcp-oauth.keys.json" }
volumes = ["data/chrome-profiles/mycompany:/home/chrome"]

[tools."gdrive.work"]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools."gdrive.work".mcp]
runtime = "docker"
image = "pynchy-mcp-gdrive:latest"
dockerfile = "src/pynchy/agent/mcp/gdrive.Dockerfile"
port = 3101
transport = "streamable_http"
env = { GDRIVE_OAUTH_PATH = "/home/chrome/gcp-oauth.keys.json" }
volumes = ["data/chrome-profiles/work:/home/chrome"]

[profiles.google]
tools = ["gdrive.mycompany", "gdrive.work"]

[workspaces.mycompany-1]
profiles = ["google"]
```

The agent sees separate tool namespaces: `mcp__gdrive_mycompany__search` and `mcp__gdrive_work__search`.

## Troubleshooting

### 403 errors from Drive API

The Drive API isn't enabled for your GCP project. Ask the agent to set up Google for the profile — it will detect the missing API and enable it.

### Token expired / authentication errors

Ask the agent to set up Google for the profile again. `setup_google` is idempotent — it detects that credentials exist but tokens are expired, and runs only the OAuth flow.

### noVNC not loading

Make sure `xvfb`, `x11vnc`, and `novnc` are installed on the host. The setup tools start the virtual display automatically, but the packages must be present.

### Browser lock files after crash

```bash
rm -f data/playwright-profiles/google/SingletonLock
rm -f data/playwright-profiles/google/SingletonSocket
rm -f data/playwright-profiles/google/SingletonCookie
```

## Operational canary

Drive is authorized with the read-only scope, so its operational check never
creates or edits a file. Keep one harmless permanent fixture in Drive and add
its ID plus a restrictive search query to `[canary]`:

```toml
scenario_ids = ["drive.google.round.trip"]
google_drive_server = "gdrive.mycompany"
google_drive_probe_query = "pynchy-canary-fixture"
google_drive_file_id = "your-fixture-file-id"
```

The canary establishes a real MCP session, checks that the server still
publishes `gdrive_search` and `gdrive_read_file`, searches with the configured
query, and reads the fixture again through a fresh session. It stores only
redacted evidence references, never Drive content.

## Migration from earlier gdrive setup

If you previously used a Docker named volume for Google Drive credentials:

1. Create the chrome profile directory and move credentials:
   ```bash
   mkdir -p data/chrome-profiles/mycompany
   cp data/gcp-oauth.keys.json data/chrome-profiles/mycompany/gcp-oauth.keys.json
   ```

2. Update `config.toml`:
   - Add `chrome_profiles = ["mycompany"]`
   - Add `[tools."gdrive.mycompany"]` and `[tools."gdrive.mycompany".mcp]`
   - Mount `data/chrome-profiles/mycompany:/home/chrome`
   - Select the tool through a profile: `tools = ["gdrive.mycompany"]`

3. Re-authorize (tokens in the old Docker volume won't carry over):
   ```
   @Pynchy set up Google for the mycompany profile
   ```

4. Clean up old artifacts:
   ```bash
   docker volume rm mcp-gdrive 2>/dev/null
   rm data/gcp-oauth.keys.json
   ```
