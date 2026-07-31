# KeePassXC Secret Enrollment

**Status:** Proposed specification.

**Outcome:** Let an operator publish a tool credential from any trusted tailnet
device into a dedicated KeePassXC database, then make the credential available
to the affected Pynchy workspace on its next turn without restarting Pynchy.

## Decision

Use a small local `pynchy-secrets` broker with a lifecycle independent from the
Pynchy service. The broker owns a dedicated `Pynchy.kdbx`, remains locked after
host reboot, and retains unlock material only in memory after an operator unlocks
it through a local or SSH terminal.

Expose the broker's enrollment page through Tailscale Serve. Keep the backend
bound to loopback, require an allowed Tailscale user identity, and require a
short-lived one-time enrollment capability. Never use Tailscale Funnel.

Keep TOML as the credential authority:

- A selected tool's `required_env` and `optional_env` declare which environment
  names a workspace may receive.
- A tool-level `credential_source` chooses either the existing process
  environment or KeePassXC for all of that tool's declared names.
- KeePassXC metadata selects which stored value satisfies an already-authorized
  environment name.
- A KeePassXC tag, enrollment link, skill, or agent request cannot authorize a
  new environment name.

Do not add a generic secret-provider hook in the first implementation. Build one
narrow built-in KeePassXC integration and extract a provider contract only when
a second backend needs the same boundary.

## User flow

```text
Missing tool requirement or operator command
                  |
                  v
Pynchy validates workspace + TOML environment name
                  |
                  v
Human-approved host action creates pending enrollment
                  |
                  v
One-time tailnet URL sent to the control conversation
                  |
                  v
Browser proves Tailscale identity and submits the secret
                  |
                  v
Broker atomically updates Pynchy.kdbx and generation marker
                  |
                  v
Next workspace turn refreshes the in-memory projection
                  |
                  v
Stale agent session and affected MCP processes retire
                  |
                  v
Replacement processes receive the new environment value
```

The normal path starts when Pynchy reports a missing requirement. The notice can
offer a **Register secret** control for each missing name. An operator may also
run the equivalent host command for a rotation or proactive registration:

```text
pynchy secrets enroll --workspace <workspace> --env <ENV_NAME>
```

Both surfaces call the same use case. The host accepts the request only when:

1. the workspace exists;
2. its resolved profiles select an enabled KeePassXC-sourced tool that declares
   `<ENV_NAME>`;
3. the invoking chat control or CLI caller has operator authority; and
4. the operator approves agent-originated requests through the existing
   exact-request approval boundary.

The agent receives only a value-free result such as `enrollment link sent`.
Pynchy sends the link through the bound control conversation without placing it
in model context or tool output.

## Enrollment URL

Do not encode the workspace, environment name, tags, or secret as query
parameters. URLs reach browser history, copied chat messages, reverse-proxy
logs, and referrer handling, and client-controlled fields can change the
intended target.

Use this shape:

```text
https://<tailnet-host>/secrets/enroll#<random-capability>
```

The fragment prevents the browser from sending the capability in the initial
HTTP request. A small first-party script reads the fragment, immediately removes
it with `history.replaceState`, and exchanges it in a POST body. The server then
binds an `HttpOnly`, `Secure`, `SameSite=Strict` enrollment cookie to the
Tailscale login and pending enrollment.

The capability contract requires:

- 256 random bits from a cryptographic random generator;
- only a SHA-256 digest persisted by the broker;
- a ten-minute default lifetime;
- one browser claim and one successful submission;
- no token in access logs, audit rows, response bodies, or referrers; and
- terminal failure after expiry, cancellation, or successful use.

Claiming a capability does not consume the enrollment immediately. It prevents
a second browser session from claiming it and gives the first session the
remaining lifetime to submit. Closing the browser requires minting another link
after the claim expires or gets cancelled.

## Tailnet boundary

Run one persistent Tailscale Serve mapping rather than changing Serve state for
every enrollment. For example, mount the broker at `/secrets` while preserving
any existing Serve handlers:

```bash
tailscale serve --bg --set-path=/secrets http://127.0.0.1:<broker-port>
```

The broker accepts web traffic only when all these conditions hold:

- the TCP peer uses loopback;
- Tailscale Serve supplies `Tailscale-User-Login`;
- the login appears in the configured operator allowlist; and
- the request carries a valid enrollment cookie or capability for the route.

Tailscale Serve removes spoofed identity headers before proxying and supplies
identity headers only for tailnet traffic. The loopback bind matters because a
directly reachable backend could otherwise accept forged identity headers.
Tailnet ACLs provide an additional network restriction, but the broker still
performs application-level identity and capability checks. Tagged devices lack
user identity headers and cannot enroll secrets in the first implementation.

Serve the page without third-party assets. Apply `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, a restrictive
Content Security Policy, `frame-ancestors 'none'`, an exact Origin check on
POSTs, bounded request bodies, and per-identity rate limits.

## Workspace scopes and KeePassXC tags

Pynchy derives candidate scope tags from current validated configuration:

| Tag | Meaning |
| --- | --- |
| `pynchy` | Marks an entry as part of the published Pynchy vault. |
| `pynchy:workspace:<workspace>` | Publishes only to the named workspace. |
| `pynchy:profile:<profile>` | Publishes to every workspace selecting that profile. |

The form always applies `pynchy`. It selects the current workspace tag by
default and offers the current workspace's expanded profile tags as broader
choices. A profile choice displays the workspaces it currently affects and
warns that future workspaces selecting the profile will also match it.

The operator may deselect candidate scope tags or select several of them. The
POST may not introduce a tag that the pending enrollment did not nominate.
Discord role selects and forum tags do not participate: they describe Discord
roles or thread kinds, not Pynchy credential policy.

Store one injectible environment value per KeePassXC entry:

| KeePassXC field | Content |
| --- | --- |
| Group | `Pynchy` |
| Title | Operator label, defaulting to the environment name |
| Password | The value injected for the environment name |
| Username, URL, Notes | Optional human metadata |
| Custom attribute `Pynchy.Environment` | Exact TOML environment name |
| Tags | `pynchy` plus one or more nominated scope tags |

At resolution time, Pynchy asks for entries whose `Pynchy.Environment` matches
the TOML requirement and whose scope tags intersect the workspace's derived
scope tags. Exactly one entry must match. Zero matches make the requirement
unavailable; multiple matches fail closed as an ambiguous publication.

The scope tag chooses among values but never grants the environment name. For
example, adding a `pynchy:profile:development` tag can share a value only with
workspaces whose selected tools already declare that value's environment name.

## Enrollment form

Render the resolved metadata as non-editable text:

- workspace;
- tool and environment name;
- create or rotate operation;
- nominated scope tags and their sharing impact; and
- expiry countdown.

Accept these initial fields:

- secret value using an HTML password input;
- optional title;
- optional username;
- optional URL;
- optional multiline notes; and
- the nominated scope-tag selection.

Do not return an existing value during rotation. Require a separately minted
rotation enrollment and preserve fields that the operator leaves untouched.
The first version leaves arbitrary custom-field and TOTP editing to KeePassXC.
Those values remain supported by the database and preserved during broker
writes, but they do not need a dynamic web-form editor to deliver environment
variables.

## Local broker

Run `pynchy-secrets` as a separate LaunchAgent or systemd user service. A normal
Pynchy deployment restart must not restart or relock it. The broker starts in a
locked state after host reboot and exposes two local surfaces:

1. a mode-`0600` Unix socket for unlock, status, enrollment creation, and
   workspace-scoped resolution; and
2. a loopback-only HTTP listener for the Tailscale Serve enrollment page.

`pynchy secrets unlock` prompts through the terminal and sends the master
password over the Unix socket. Never accept it through argv, an environment
variable, a URL, or a file. Python cannot promise perfect memory locking or
zeroization, so the broker makes no such claim: after unlock, an active
same-user or root compromise can recover or invoke the same secrets.

The broker configuration may use the built-in plugin's operator-owned options:

```toml
[plugins.keepassxc]
enabled = true

[plugins.keepassxc.options]
database = "data/secrets/Pynchy.kdbx"
socket = "data/secrets/keepassxc.sock"
generation_file = "data/secrets/generation"
enrollment_host = "127.0.0.1"
enrollment_port = 8490
enrollment_base_url = "https://<tailnet-host>/secrets/enroll"
allowed_tailnet_logins = ["<operator-login>"]
enrollment_ttl_seconds = 600
```

The broker reads the same validated options, while the database password never
appears in TOML. Paths remain host-only and never enter an agent or MCP mount.

Each migrated tool explicitly selects the broker in its existing declaration:

```toml
[tools.example]
type = "workspace"
required_env = ["EXAMPLE_TOKEN"]
credential_source = "keepassxc"
```

`credential_source = "environment"` remains the default for compatibility.
Source selection applies to the complete tool because mixing sources within one
small credential set adds ambiguity without a demonstrated need. Switching a
tool to KeePassXC changes its TOML definition and can use the normal managed
deployment once; later value creation and rotation use the hot path.

## KDBX write contract

Use a KDBX library for the write path. `keepassxc-cli` can read arbitrary
attributes and current TOTP values, but its documented `add` and `edit` options
cannot assign the required tags or custom attribute. PyKeePass supplies entry
creation with notes and tags and supports custom strings; pin it only after a
round-trip compatibility test against the deployed KeePassXC version.

Every mutation follows this sequence:

1. Acquire the broker's process lock.
2. Load the current database from disk instead of mutating a startup snapshot.
3. Resolve the target and reject ambiguous create or rotate operations.
4. Save a mode-`0600` timestamped backup.
5. Write a sibling temporary KDBX file and `fsync` it.
6. Verify that the source database hash has not changed since loading.
7. Atomically replace the source, then `fsync` its parent directory.
8. Record the new encrypted-file fingerprint and increment an atomic,
   non-secret generation marker.
9. Consume the enrollment and record a value-free audit event.

If the source changes before replacement, discard the temporary file, reload,
and retry once. A second conflict leaves the original untouched and asks the
operator to save or close concurrent KeePassXC edits before retrying. The
broker never writes plaintext XML or a resolved-secret sidecar.

Compatibility tests must create and enrich a database with KeePassXC, mutate it
through the broker, reopen it with KeePassXC and `keepassxc-cli`, and prove that
unrelated custom attributes, attachments, entry history, and TOTP metadata
survive.

The broker also checks the encrypted KDBX file identity, size, modification time,
and content hash when Pynchy asks for the current generation. A save performed
directly in KeePassXC advances the generation before the broker returns it. This
keeps manual edits authoritative without requiring a filesystem watcher and
without decrypting the database merely to detect a change.

## Runtime projection and hot refresh

Keep secret values outside the Pynchy host's `os.environ`. The KeePassXC
integration maintains an immutable in-memory projection keyed by workspace
scope and environment name and supplies that mapping to the existing
tool-access resolver. A KeePassXC-sourced tool never falls back to the process
environment: a locked broker, missing entry, deleted entry, or ambiguous match
makes the tool unavailable. This prevents a revoked value from silently
reappearing from stale startup state.

Before each workspace turn, Pynchy asks the broker for its non-secret generation.
The broker first detects direct KeePassXC saves as described above. When the
generation changes, Pynchy requests only the environment names declared by the
workspace's selected KeePassXC tools and applies these rules:

1. Publish the refreshed immutable projection.
2. If the current workspace's availability or value changed, retire its warm
   agent session at the queue boundary.
3. Retire each affected MCP instance after any active backend lease drains.
4. Start replacement processes with the refreshed environment.
5. Preserve conversation history, sticky security taint, unrelated sessions,
   and unrelated MCP instances.

This pull-on-next-turn contract needs no callback from the broker and recovers
when Pynchy was stopped during enrollment. A Pynchy process start also reads the
current generation. If the broker remains locked or unavailable, Pynchy starts
normally and reports the affected tools as unavailable; unlocking the broker
allows the next turn to refresh them.

The broker remains running and unlocked across an ordinary Pynchy code deploy.
A host reboot restarts it locked and requires another terminal unlock. An agent
or MCP process already executing when a rotation occurs may finish with its old
environment; no new turn starts in that workspace until the stale runtime
retires.

This flow extends the existing
[affected-workspace runtime refresh](workspace-runtime-policy-refresh.md)
principle to secret values without treating tool-definition changes as hot
reloadable configuration.

## Failure behavior

| Failure | Result |
| --- | --- |
| Broker locked | Form shows an unlock-required state without accepting a value; tools remain unavailable. |
| Enrollment expired or already claimed | Reject without revealing whether a matching secret exists. |
| Tailscale identity missing or disallowed | Return a generic forbidden response and audit only the login metadata supplied by Serve. |
| Database contains zero rotation matches | Reject rotation; never silently create a second entry. |
| Database contains multiple matches | Fail closed and identify the ambiguous entry titles without values. |
| Concurrent database change | Retry once, then preserve the original and request operator intervention. |
| KDBX write succeeds but Pynchy remains down | Preserve the new generation; Pynchy discovers it at startup or the next turn. |
| MCP retirement waits on an active call | Let the call finish, prevent a replacement turn from starting, then retire and recreate it. |
| Projection refresh fails | Keep the previous runtime unavailable for a new turn rather than mixing old and new credential state. |

## Security audit

Record these value-free events:

- enrollment requested, approved, claimed, expired, cancelled, and consumed;
- requesting workspace, tool, environment name, nominated scopes, and selected
  scopes;
- authenticated Tailscale login;
- KDBX generation and create-versus-rotate result; and
- affected runtime retirement and refresh outcome.

Never record the capability, cookie, database password, submitted secret,
username, URL, notes, TOTP seed, KDBX decrypted content, subprocess output, or
resolved process environment.

## Non-goals

- Exposing the complete personal KeePassXC database to Pynchy. Use a dedicated
  published database even when another KeePassXC database stores the full vault.
- Building a replacement KeePassXC editor in the browser.
- Editing arbitrary custom fields, attachments, or TOTP configuration through
  the first enrollment form.
- Automatically unlocking after host reboot with a colocated unattended key.
- Using Discord roles or forum tags as credential policy.
- Putting mutable metadata or capabilities in URL query parameters.
- Migrating boot-critical channel, gateway, or control-plane credentials in the
  first slice. The initial resolver covers credentials injected into selected
  workspace and MCP tool processes.

## Delivery slices

1. Implement the locked broker, Unix socket, KDBX read/write transaction, CLI
   unlock, backups, generation marker, and compatibility tests.
2. Add the workspace-scoped in-memory projection and next-turn agent/MCP
   retirement without changing current TOML tool authority.
3. Add the loopback enrollment server, Tailscale identity checks, fragment-token
   exchange, static form, and browser security tests.
4. Add the approved host action, missing-requirement control, operator CLI, and
   value-free audit events.
5. Consider TOTP or arbitrary-field enrollment only after the password/token
   path operates reliably.

## Acceptance criteria

- [ ] An enrollment can target only an environment name declared by a selected,
      KeePassXC-sourced TOML tool in the resolved workspace.
- [ ] The generated URL contains only a fragment capability; workspace, tags,
      environment name, and secret remain absent from the URL.
- [ ] Only an allowed Tailscale user can claim and submit an unexpired link.
- [ ] The server accepts only the tags nominated from the current workspace and
      resolved profiles, with the workspace tag selected by default.
- [ ] A completed enrollment creates or rotates one KeePassXC entry without
      exposing any submitted value in logs, SQLite, process argv, or audit data.
- [ ] KeePassXC and `keepassxc-cli` reopen the mutated database with unrelated
      TOTP, attributes, attachments, and history intact.
- [ ] The next affected workspace turn receives the value without restarting
      Pynchy, and any stale MCP instance receives retirement first.
- [ ] Direct KeePassXC edits, deletions, and tag changes advance the broker
      generation and never fall back to a stale process-environment value.
- [ ] Unrelated workspaces, MCP instances, conversation history, and security
      taint remain unchanged.
- [ ] A Pynchy deployment restart leaves the unlocked broker alive; a host
      reboot leaves it locked.
- [ ] Zero-match, duplicate-match, expiry, replay, concurrent-save, locked-vault,
      and refresh-failure paths fail closed without losing database data.

## External constraints

- [KeePassXC CLI manual](https://raw.githubusercontent.com/keepassxreboot/keepassxc/develop/docs/man/keepassxc-cli.1.adoc)
- [KeePassXC User Guide](https://keepassxc.org/docs/KeePassXC_UserGuide)
- [PyKeePass documentation](https://pykeepass.readthedocs.io/en/latest/index.html)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale Serve command](https://tailscale.com/docs/reference/tailscale-cli/serve)
