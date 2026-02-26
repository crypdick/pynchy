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

## 1. Define chrome profile and instance

Add a chrome profile and gdrive instance to `config.toml`:

```toml
chrome_profiles = ["mycompany"]

[mcp_servers.gdrive.mycompany]
chrome_profile = "mycompany"
```

The plugin provides the base spec (Docker image, port, transport, Dockerfile). You only declare the instance with its chrome profile attachment.

## 2. Grant workspace access

```toml
[workspaces.mycompany-1]
mcp_servers = ["gdrive.mycompany"]
```

## 3. First-time setup

Ask your agent to set up Google for the profile:

```
@Pynchy set up Google for the mycompany profile
```

The agent calls `setup_google(chrome_profile="mycompany")`, which automates the full GCP setup flow: create a project, enable the Drive API, configure OAuth consent, create credentials, and run the OAuth authorization. You click "Allow" on the Google consent screen to grant read-only Drive access.

On a **headless server**, the agent returns a noVNC URL — open it in your browser to interact with the GCP Console and Google login.

## 4. Verify

Trigger a message in a workspace with `gdrive.mycompany` access. The Docker container starts on-demand:

```bash
ssh pynchy-server 'docker ps --filter name=pynchy-mcp-gdrive'
```

## Multiple accounts

Each chrome profile maps to one Google account. To access Drive from multiple accounts:

```toml
chrome_profiles = ["mycompany", "work"]

[mcp_servers.gdrive.mycompany]
chrome_profile = "mycompany"

[mcp_servers.gdrive.work]
chrome_profile = "work"

[workspaces.mycompany-1]
mcp_servers = ["gdrive.mycompany", "gdrive.work"]
```

The agent sees separate tool namespaces: `mcp__gdrive_mycompany__search` and `mcp__gdrive_work__search`.

## Troubleshooting

### 403 errors from Drive API

The Drive API isn't enabled for your GCP project. Ask the agent to set up Google for the profile — it will detect the missing API and enable it.

### Token expired / authentication errors

Ask the agent to set up Google for the profile again. The `setup_google` tool is idempotent — it detects that credentials exist but tokens are expired, and runs only the OAuth flow.

### noVNC not loading

Ensure `xvfb`, `x11vnc`, and `novnc` are installed on the host. The setup tools start the virtual display automatically, but the packages must be present.

### Browser lock files after crash

```bash
rm -f data/playwright-profiles/google/SingletonLock
rm -f data/playwright-profiles/google/SingletonSocket
rm -f data/playwright-profiles/google/SingletonCookie
```

## Migration from old gdrive setup

If you previously used the old `[mcp_servers.gdrive]` config with Docker named volumes:

1. Create the chrome profile directory and move credentials:
   ```bash
   mkdir -p data/chrome-profiles/mycompany
   cp data/gcp-oauth.keys.json data/chrome-profiles/mycompany/gcp-oauth.keys.json
   ```

2. Update `config.toml`:
   - Add `chrome_profiles = ["mycompany"]`
   - Remove the old `[mcp_servers.gdrive]` section (plugin provides it now)
   - Add instance: `[mcp_servers.gdrive.mycompany]` with `chrome_profile = "mycompany"`
   - Update workspace: `mcp_servers = ["gdrive.mycompany"]`

3. Re-authorize (tokens in the old Docker volume won't carry over):
   ```
   @Pynchy set up Google for the mycompany profile
   ```

4. Clean up old artifacts:
   ```bash
   docker volume rm mcp-gdrive 2>/dev/null
   rm data/gcp-oauth.keys.json
   ```
