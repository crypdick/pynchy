# Channel-scoped secrets

Grant a Discord channel access to named Vaultwarden collections. Child Discord
threads and semantic workspaces inherit the physical parent channel's grant.
Channels without a grant do not receive the `get_secret` tool.

## Configure collection access

Keep collection names and UUIDs in the private personalization repository:

```toml
[connections.synapse.chat.pynchy.channels.finance]
name = "finance"
secret_collections = ["finance"]

[connections.synapse.chat.pynchy.channels.systems]
name = "systems"
secret_collections = ["systems", "shared"]

[plugins.vaultwarden.options]
server_url = "https://pynchy-vaultwarden.pynchy.svc.cluster.local"

[plugins.vaultwarden.options.collections]
finance = "11111111-1111-4111-8111-111111111111"
systems = "22222222-2222-4222-8222-222222222222"
shared = "33333333-3333-4333-8333-333333333333"
```

Pynchy rejects unknown collection names during configuration validation. Do not
add a `vaultwarden` tool declaration or profile tool grant. Channel collection
access installs the internal grant automatically and marks that workspace as
containing secrets.

Use one dedicated Vaultwarden member account per secret-enabled channel. Give
that account access to exactly the collections declared on the channel. Mount
these environment values only into the trusted Pynchy host process, replacing
`FINANCE` with the uppercase channel key:

```text
PYNCHY_VAULTWARDEN_FINANCE_EMAIL
PYNCHY_VAULTWARDEN_FINANCE_PASSWORD
```

The broker passes the password through `BW_PASSWORD` and Bitwarden CLI's
`--passwordenv` option. It never puts the password in process arguments.

## Retrieve a login

Call `get_secret` with an exact Vaultwarden item name. The host searches only
the channel's collection UUIDs and requires one exact name match. It returns a
path such as `/tmp/pynchy-secrets/abc.json` plus available key names. It never
returns field values through the tool response.

The JSON file contains supported login fields such as `email`, `login`, and
`password`. Pynchy writes it with mode `0600`, mounts only that channel runtime's
directory, and removes files when the runtime exits. TOTP values are excluded;
ask the operator when a site requires one.

The same channel grant also installs one channel-specific Playwright browser.
Its headed Chromium process uses a host-only persistent profile, the official
Bitwarden extension installed by managed browser policy, and a shared browser
context. All child workspaces select the same MCP instance and profile. The
browser tool retains normal web trust and approval gates; use Bitwarden's own
autofill rather than copying passwords into browser tool arguments.

The Bitwarden CLI and its session key stay in the host container. Configure only
a Kubernetes service origin ending in `.svc.cluster.local`. Startup and requests
fail closed when the configured server differs from the CLI profile, credentials
are missing, collection IDs are invalid, or the provider returns malformed data.

## Administer the vault

Bind a locked Discord channel to a dedicated workspace and grant only that
workspace the `vaultwarden-admin` tool:

```toml
[profiles.secrets-admin]
tools = ["vaultwarden-admin"]
contains_secrets = true

# data/personalization/workspaces/secrets.toml
schema_version = 1

[workspace]
profiles = ["secrets-admin"]
chat = "connection.discord.synapse.chat.example.channels.secrets"
permissions = { ask = ["secret.vaultwarden.admin"] }
```

Mount `PYNCHY_VAULTWARDEN_ADMIN_EMAIL` and
`PYNCHY_VAULTWARDEN_ADMIN_PASSWORD` only into the trusted host process. Every
administration request requires exact human approval. The tool can:

- verify each channel account sees exactly its configured collections;
- create or update login items from another exact-name vault item or a protected
  host file;
- assign one item to multiple collections;
- create collections and grant them to selected channel member accounts; and
- change a channel's collection list while updating member grants and private
  personalization configuration together.

Never pass usernames, passwords, TOTP values, or other secret values in tool
arguments or Discord messages. Browser-captured logins already exist as vault
items and need only collection assignment. For protected local input, place one
JSON login object in `data/vaultwarden-admin-input/` with mode `0600`, then pass
only its basename. The broker excludes TOTP when it copies an item.

Use normal Vaultwarden pages only for initial account creation and invitation
acceptance. Use the administration tool for later item, collection, grant, and
access-verification work.

## Deploy Vaultwarden

The K3s application pins Vaultwarden and the host image pins Bitwarden CLI.
Before applying the application, provision the deployment-specific
`pynchy-vaultwarden` PVC, a `pynchy-vaultwarden` Secret with a `DOMAIN` key, and
a `pynchy-vaultwarden-tls` TLS Secret. Enable K3s Secret encryption and rotate
existing Secret data before mounting channel account credentials into the
Pynchy host container.

Keep the supplied `ClusterIP` Service private to the cluster. Do not add Ingress,
NodePort, LoadBalancer, or tunnel exposure.

Provision `pynchy-vaultwarden-ca` with the issuing CA certificate in a `ca.crt`
key. Also provision `pynchy-bitwarden-policy` with a `bitwarden.json` key. Use
Chromium's `ExtensionInstallForcelist` for extension ID
`nngceckbapebfimnlniiiahkandclblb`, and set that extension's
`3rdparty.extensions` `environment.base` policy to the same HTTPS Vaultwarden
origin. Add the issuing CA to Chromium's `CACertificates` policy.

Keep Vaultwarden's `/data` volume on retained, backed-up storage. The supplied
K3s backup script copies attachments and makes a consistent SQLite backup when
the Vaultwarden database exists.
