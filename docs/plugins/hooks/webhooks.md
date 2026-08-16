# Webhook hook

## `pynchy_webhook_routes`

Provide a provider-authenticated HTTP callback. The plugin owns provider schema,
signature verification, replay detection, and the closed mapping from provider
events to a scheduled task, routed conversation, lifecycle-only callback, host
notification, ignored result, or pre-admission discard. The host owns the public
path, size/rate limits, workspace boundary, durable receipt, and dispatch.

```python
class ExamplePlugin:
    @hookimpl
    def pynchy_webhook_routes(self) -> WebhookRoute:
        return WebhookRoute(
            provider="example",
            name="project",
            workspace="project",
            secret_env="EXAMPLE_WEBHOOK_SECRET",  # pragma: allowlist secret
            parse=parse_example,
            public_source=True,
        )
```

Pynchy collects and validates routes during HTTP startup. A plugin can return one
`WebhookRoute`, a tuple, or `None`. The resulting endpoint is
`POST /webhooks/<provider>/<name>`. Provider and route names must be lowercase
URL-safe identifiers. Admin targets require a trusted source policy and explicit
`allow_admin_workspaces=True` opt-in.

A provider that resolves ownership from authenticated event metadata may set
`workspace=None` and declare every allowed logical owner in
`candidate_workspaces`. Its `prepare_event` callback must then return a
`WebhookConversation.workspace` from that allowlist. Set
`allow_admin_workspaces=True` only when the provider identity and per-candidate
source-trust declarations make admin admission safe. The host validates all fixed
targets and candidates at startup and checks resolved owners again for every
delivery.

Parse raw request bytes because signatures commonly cover the exact body.
Authenticate before parsing; raise `WebhookAuthenticationError` for bad
credentials or replay checks, and `WebhookPayloadError` only for an authenticated
payload that fails its schema. Return `WebhookDiscard` after authentication when a
delivery must leave no durable receipt or host effect. The host responds with
`204` before workspace resolution, preparation, and admission. Return an ignored
`WebhookEvent` instead when operators need a durable audit or replay record. Keep
provider text in `external_context`, separate from host-authored instructions. A
route can provide a concise, provider-rendered string or a mapping that the host
serializes. `public_source=True` remains the safe default and makes the host fence
that context. Set `public_source=False` only when every provider principal who can
contribute the routed content belongs inside the workspace trust boundary.

Use `prepare_event` for a read-only provider check that must run before receipt or
conversation admission. The host runs preparation on delivery replays, so the
callback must remain idempotent. Use `process_event` for an idempotent host effect
that runs only before the first receipt admission. It must return the resulting
`WebhookEvent`; a provider may return a new event with a different disposition
when the host effect transfers execution ownership to another durable subsystem.

Set `routes_conversations=True` when actionable events target durable subjects.
Return a `WebhookConversation` with an immutable `ConversationSubject` and a
readable control title. `control_closed=True` marks terminal control intent for
a lifecycle-only event, `False` explicitly reopens normal conversation handling,
and `None` preserves current terminal intent. The channel owns the native
mapping; Discord maps terminal controls to archived threads.

## Lifecycle-only callbacks

Use `WebhookLifecycle` when a provider callback must change conversation
lifecycle state without prompting the agent. A lifecycle event must omit
`instructions` and `external_context`, and it must carry a `WebhookConversation`
with `control_closed=True`. Its optional `context` must contain JSON-serializable,
provider-owned data rather than prompt content.

Set `WebhookRoute.process_lifecycle` to an async callback that accepts a
`WebhookLifecycleDelivery`. The host persists a `lifecycle` receipt and joins the
delivery to the subject's FIFO. At ingress, it records terminal intent, retires
older routed work and runtime ownership, clears the routed session, and archives
an existing control. It invokes the route callback with the immutable delivery
identity, subject, workspace, and context, then completes the delivery. A
lifecycle-only delivery does not construct a `NewMessage`, an agent turn, a
runtime workspace, or a first-time control thread.

Lifecycle callbacks have at-least-once delivery. If archiving or the callback
fails, or the host stops after the callback but before delivery completion,
recovery retries the lifecycle delivery. Make provider effects idempotent with
`WebhookLifecycleDelivery.identity`; do not depend on a callback running exactly
once. A callback must also tolerate a control that already closed during an
earlier attempt.
