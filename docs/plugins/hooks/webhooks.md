# Webhook hook

## `pynchy_webhook_routes`

Provide a provider-authenticated HTTP callback. The plugin owns provider schema,
signature verification, replay detection, and the closed mapping from provider
events to an isolated task, routed conversation, host notification, or ignored
result. The host owns the public path, size/rate limits, workspace boundary,
durable receipt, and dispatch.

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
URL-safe identifiers, and a fixed target cannot be an admin workspace.

A provider that resolves ownership from authenticated event metadata may set
`workspace=None` and declare every allowed logical owner in
`candidate_workspaces`. Its `prepare_event` callback must then return a
`WebhookConversation.workspace` from that allowlist. Set
`allow_admin_workspaces=True` only when the provider identity and per-candidate
source-trust declarations make admin admission safe; fixed routes remain barred
from admin workspaces by default. The host validates all candidates at startup
and checks the resolved owner again for every delivery.

Parse raw request bytes because signatures commonly cover the exact body.
Authenticate before parsing; raise `WebhookAuthenticationError` for bad
credentials or replay checks, and `WebhookPayloadError` only for an authenticated
payload that fails its schema. Keep provider text in `external_context`, separate
from host-authored instructions. A route can provide a concise, provider-rendered
string or a mapping that the host serializes. `public_source=True` remains the safe
default and makes the host fence that context. Set `public_source=False` only when
every provider principal who can contribute the routed content belongs inside the
workspace trust boundary.

Use `prepare_event` for a read-only provider check that must run before receipt or
conversation admission. The host runs preparation on delivery replays, so the
callback must remain idempotent. Use `process_event` for an idempotent host effect
that runs only before the first receipt admission.

Set `routes_conversations=True` when actionable events target durable subjects.
Return a `WebhookConversation` with an immutable `ConversationSubject` and a
readable control title. `control_closed=True` requests a closed control after
the routed turn completes, `False` requests an open control, and `None` preserves
the conversation's persisted lifecycle intent. The channel owns the native
mapping; Discord maps closed controls to archived threads.
