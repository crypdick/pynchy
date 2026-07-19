# Google integrations

Set up Google services through a Chrome profile, then enable the service tools a
workspace needs. Google Drive and Calendar use the same profile and OAuth setup;
Google Workspace via Gog uses a separate host-only OAuth flow.

## Choose a service

| Service | What it does |
|---------|--------------|
| [Google Drive](drive.md) | Search, list, and read Drive files. |
| [Google Calendar](calendar.md) | Read and manage calendar events. |
| [Google Workspace via Gog](workspace-gog.md) | Use Gmail, Contacts, Docs, and Sheets through reviewed host actions. |

## Prepare a Chrome profile

Google Drive and Calendar use a Chrome profile for the Google Cloud setup and
OAuth state. On the Pynchy host, install system Chrome. Headless hosts also need
a virtual display for interactive Google consent:

```bash
apt install google-chrome-stable
apt install xvfb x11vnc novnc
```

Set `CHROME_PATH` in `.env` when Chrome does not use the default executable:

```text
CHROME_PATH=/usr/bin/google-chrome-stable
```

## Authorize Drive or Calendar

First configure one or both service tools from the service guide. Then ask an
agent to set up Google for that Chrome profile:

```text
@Pynchy set up Google for the mycompany profile
```

`setup_google(chrome_profile="mycompany")` creates missing Google Cloud
resources, enables the required APIs, and starts OAuth authorization. It derives
the required scopes from the Drive and Calendar tools that use the profile. On a
headless host, it returns a noVNC URL for the Google sign-in and consent steps.

## Use multiple Google accounts

Each Chrome profile maps to one Google account. For each account, declare a
separate Chrome profile, create one Drive and/or Calendar tool with a unique MCP
port, and select those tools in a workspace profile. Agents then see separate
tool namespaces such as `mcp__gdrive_mycompany__search` and
`mcp__gcal_personal__list_events`.

Gog does not use these Chrome profiles. Its OAuth client and account stay in a
host-only Gog home; see [Google Workspace via Gog](workspace-gog.md).

---

**Want to customize this?** Write your own plugin — see the [Plugin Authoring Guide](../../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
