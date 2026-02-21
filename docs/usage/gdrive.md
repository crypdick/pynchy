# Google Drive

Gives agents read-only access to your Google Drive files. Agents can search, list, and read documents, spreadsheets, and other Drive content.

## Prerequisites

On the host machine (pynchy-server):

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

## 1. Define the MCP server

Add the gdrive MCP server to `config.toml`:

```toml
[mcp_servers.gdrive]
type = "docker"
image = "pynchy-mcp-gdrive:latest"
dockerfile = "container/mcp/gdrive.Dockerfile"
port = 3000
transport = "streamable_http"
volumes = [
    "mcp-gdrive:/gdrive-server",
    "data/gcp-oauth.keys.json:/app/gcp-oauth.keys.json:ro",
]
```

## 2. Grant workspace access

```toml
[workspaces.my-workspace]
mcp_servers = ["gdrive"]
```

## 3. First-time setup

Ask your agent to set up Google Drive:

```
@Pynchy set up Google Drive access
```

The agent automates the full GCP setup flow: create a project, enable the Drive API, configure OAuth consent, create credentials, and run the OAuth authorization. You click "Allow" on the Google consent screen to grant read-only Drive access.

On a **headless server**, the agent returns a noVNC URL — open it in your browser to interact with the GCP Console and Google login.

## 4. Verify

Trigger a message in a workspace with `gdrive` access. The Docker container starts on-demand:

```bash
ssh pynchy-server 'docker ps --filter name=pynchy-mcp-gdrive'
```

## Troubleshooting

### 403 errors from Drive API

The Drive API isn't enabled for your GCP project. Ask the agent to enable it, or do it manually in the [GCP Console](https://console.cloud.google.com/apis/library/drive.googleapis.com).

### Token expired / authentication errors

Ask the agent to re-authorize Google Drive. It opens the consent screen via noVNC for you to click "Allow" again.

### noVNC not loading

Ensure `xvfb`, `x11vnc`, and `novnc` are installed on the host. The setup tools start the virtual display automatically, but the packages must be present.

### Browser lock files after crash

```bash
rm -f data/playwright-profiles/google/SingletonLock
rm -f data/playwright-profiles/google/SingletonSocket
rm -f data/playwright-profiles/google/SingletonCookie
```
