# GitHub PR notifications

The built-in GitHub webhook plugin sends pull-request updates directly to the
mapped project workspace. Most events are concise human-visible notifications.
A top-level comment, review, inline review comment, or single-PR check failure on
a PR attached to one managed Linear issue wakes that issue's existing conversation
and worktree for follow-up.
A merged pull request starts an isolated agent turn so the agent can inspect
the linked work and exercise judgment about Follow-ups. The webhook itself
doesn't create another worktree or mutate GitHub or Linear. A route binds one GitHub
repository to one Pynchy workspace, so an event can never fall back to an unrelated
channel.

## Configure repository routes

Add one route for each repository and its owning workspace:

```toml
[[plugins.github.options.webhook_routes]]
name = "project"
workspace = "project-workspace"
repository = "owner/project"
secret_env = "GITHUB_PROJECT_WEBHOOK_SECRET" # pragma: allowlist secret
```

Store the corresponding webhook secret in the host environment, not in
`data/personalization/pynchy.toml`:

```bash
GITHUB_PROJECT_WEBHOOK_SECRET="replace-with-a-random-webhook-secret" # pragma: allowlist secret
```

The route becomes `POST /webhooks/github/project`. It accepts a delivery only when
its `repository.full_name` exactly matches `owner/project`; a valid signature for a
different repository still receives `400` and cannot be routed anywhere. Configure
a separate route and secret for every project that should notify a different
workspace.

### Trust selected GitHub senders

Routes treat GitHub content as public input by default and cannot target an admin
workspace. To accept PR feedback only from trusted GitHub accounts, add an explicit
sender allowlist:

```toml
allowed_senders = ["repo-owner"]
```

Pynchy verifies the webhook signature, then compares `sender.login`
case-insensitively. It returns `204` for missing or unlisted senders without
storing a receipt, resolving a workspace, or waking an agent. An allowlisted route
treats accepted content as trusted and may target an admin workspace. Include
every account whose comments belong inside that workspace's trust boundary; omit
this option for public review.

## Create the GitHub webhook

Pynchy must have a public HTTPS URL before GitHub can reach this endpoint. Configure
the control-plane listener with its public-bind safeguards, then place a TLS reverse
proxy or tunnel in front of it. Do not expose the unauthenticated loopback listener
directly; see [Control plane](../usage/control-plane.md#provider-authenticated-webhooks).

In the repository's **Settings → Webhooks → Add webhook** form, configure:

| Field | Value |
|---|---|
| Payload URL | `https://pynchy.example.com/webhooks/github/project` |
| Content type | `application/json` |
| Secret | The value of `GITHUB_PROJECT_WEBHOOK_SECRET` |
| Active | Enabled |
| Events | Pull requests, Issue comments, Pull request reviews, Pull request review comments, and Check runs |

GitHub delivers a GUID in `X-GitHub-Delivery` and a SHA-256 HMAC in
`X-Hub-Signature-256`; Pynchy authenticates the raw bytes, deduplicates that GUID,
and records only receipt metadata plus a body digest. The route accepts up to 25 MiB
because that is GitHub's documented maximum webhook payload. It remains a hard
limit—GitHub will not deliver payloads larger than that maximum.

## What the project channel receives

The plugin emits concise direct host notifications for:

- New commits, PR lifecycle updates, title changes, and description changes.
- Approved or dismissed reviews.
- Failed check runs associated with several pull requests.
- An explicit non-mergeable state included in a pull-request delivery.

Development agents include every pull request URL in the `evidence_refs` for
their `Awaiting Review` outcome. The transition creates the native Linear
attachment before changing state. For new or edited top-level PR comments,
actionable submitted or edited reviews, inline review comments, and a failed check
associated with one PR, the webhook resolves that exact attachment. One matching
issue on the route's workspace receives the event in its canonical Linear
conversation. The agent fetches current review details, triages them, applies
warranted changes in the existing worktree, and runs local CI. It doesn't rerun
GitHub CI, merge, or deploy solely because of the event. Missing, ambiguous,
off-board, or unconfigured Linear links fall back to a direct workspace
notification instead of creating a separate agent turn.

A merged-PR event starts an isolated agent turn, which resolves the URL with
`linear_find_issues_by_attachment_url`, inspects the linked work and runtime
state, and decides whether the job should enter `Follow-ups`, `Done`, or remain
elsewhere. Merge is evidence, not an automatic state transition.

GitHub does not publish a dedicated merge-conflict event, and mergeability can be
computed asynchronously. The webhook immediately reports the commit event; retain a
low-frequency read-only reconciliation if every merge-conflict transition must be
detected even when GitHub does not include a non-mergeable state in the delivery.

The plugin deliberately ignores non-PR issue comments, successful checks, checks
that GitHub cannot associate with a PR, and event types outside the configured
read-only scout surface. This avoids turning a project channel into a repository
firehose.

For the exact event headers, payload limits, and event availability, see GitHub's
[webhook event documentation](https://docs.github.com/en/webhooks/webhook-events-and-payloads).
