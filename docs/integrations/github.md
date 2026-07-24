# GitHub PR notifications

The built-in GitHub webhook plugin sends pull-request updates directly to the
mapped project workspace. It does not start an agent, create a worktree, or write
to GitHub. For development work that links a pull request as review evidence, an
authenticated merge delivery also moves the Linear item from `Awaiting Review`
to `Done`. Other kinds of work can use different evidence and human acceptance.
A route binds one GitHub repository to one Pynchy workspace, so an event can
never fall back to an admin or unrelated channel.

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
- New or edited PR conversation and inline review comments.
- Submitted, edited, or dismissed reviews.
- Failed check runs that GitHub associates with one or more pull requests.
- An explicit non-mergeable state included in a pull-request delivery.

A merged pull request is also a trusted lifecycle signal for the built-in
[Linear integration](linear.md). Pynchy matches the canonical PR URL stored by
`linear_await_review_work_item`, records the Linear transition before applying
it, and completes only an execution already in `Awaiting Review`. Duplicate
GitHub delivery IDs do not repeat the transition. A PR with no linked Pynchy
execution remains a notification only.

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
