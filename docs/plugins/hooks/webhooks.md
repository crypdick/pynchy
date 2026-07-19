# Webhook hook

## `pynchy_webhook_routes`

Provide a provider-authenticated HTTP callback. The plugin owns provider schema,
signature verification, replay detection, and the closed mapping from provider
events to a task, host notification, or ignored result. The host owns the public
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
        )
```

Pynchy collects and validates routes during HTTP startup. A plugin can return one
`WebhookRoute`, a tuple, or `None`. The resulting endpoint is
`POST /webhooks/<provider>/<name>`. Provider and route names must be lowercase
URL-safe identifiers, and the target cannot be an admin workspace.

Parse raw request bytes because signatures commonly cover the exact body.
Authenticate before parsing; raise `WebhookAuthenticationError` for bad
credentials or replay checks, and `WebhookPayloadError` only for an authenticated
payload that fails its schema. Do not copy provider text into trusted task
instructions. The host fences external context before building the task prompt.
