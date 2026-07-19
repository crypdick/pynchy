# Google Workspace via Gog

Gog gives a workspace a narrow, host-side Google Workspace surface: Gmail search,
sanitized message reads, draft creation and sending, Contacts search, Google Docs
reads and exports, and Google Sheets reads and range updates. It does not expose a
generic `gog` shell command, a selectable Google account, or a credential mount to
the agent container.

Gog complements the existing [Google Drive](drive.md) MCP integration; it does
not replace it.

## Install and configure

Install the [Gog CLI](https://gogcli.sh/quickstart.html) on the Pynchy host, then
configure one account and one host-only Gog home. Create a Google OAuth Desktop
client in Google Cloud and keep its JSON file outside any workspace mount.

```toml
[plugins.gog.options]
account = "you@example.com"
home = "data/gog"
oauth_client_path = "data/google/gog-oauth-client.json"
timeout_seconds = 60

[tools.gog]
type = "builtin"
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[profiles.google-workspace]
tools = ["gog"]
contains_secrets = true

[workspaces.personal]
profiles = ["google-workspace"]
```

`home` holds Gog's OAuth state on the host. Pynchy never mounts it or the OAuth
client JSON into the agent container. `account` is configuration, not a tool
argument, so an agent cannot switch to another signed-in account.

## Authorize

From a workspace that selects `gog`, ask Pynchy to start Gog setup. It returns the
Google authorization URL. Open it as the configured account, complete consent,
then give Pynchy the final redirect URL so it can complete setup.

Pynchy fixes this integration's Gog services to Gmail, Contacts, Docs, Sheets, and
read-only Drive. Gmail write access is necessary for drafts and sending; Drive is
read-only and is used only to support document export. Calendar and generic Drive
actions are not part of this integration.

The flow uses Gog's [remote two-step authorization](https://gogcli.sh/commands/gog-auth-add.html),
which keeps OAuth state on the host.

### Google Advanced Protection

Google Advanced Protection blocks unverified third-party OAuth clients from
accessing Gmail, Drive, Contacts, and other sensitive Google data. If Google
shows `Error 400: policy_enforced` or says that the OAuth app lacks Advanced
Protection approval, Pynchy cannot complete Gog authorization for that account.

Advanced Protection does not provide a personal allowlist for an unverified
client. Keep Advanced Protection enabled. To use Gog with that account, submit
the OAuth app for [Google OAuth app verification](https://support.google.com/cloud/answer/13461325).
Google requires app identity details, scope justification, and an end-to-end
demo; restricted scopes can also require a security assessment. Until Google
approves the app, use an account that does not enroll in Advanced Protection.

## Operations and approval

The agent can use these reviewed operations:

- Gmail: search, sanitized message read, create draft, send draft, send message
- Contacts: search
- Docs: read text and export as text, Markdown, or HTML
- Sheets: get a range and update a range

Search and read results are treated as untrusted content before they reach the
agent. Creating drafts, sending mail, and updating Sheets are write actions. With
the recommended `dangerous_writes = true`, Pynchy's normal approval and durable
action-intent lifecycle applies to every one of them, including draft creation.

Gog does not receive agent-selected command-line flags. The host wrapper builds a
fixed command allowlist, passes mail bodies and sheet values on standard input, and
uses Gog's read-only safeguards for every read operation.

## Verify and troubleshoot

Run `pynchy doctor --workspace personal` after configuring the workspace. The Gog
capability check verifies only local configuration and the executable; it does not
contact Google. If it is unavailable, check that `gog` is installed, the configured
account is present, and the OAuth client path exists on the host. Re-run the setup
flow to refresh an expired OAuth session.
