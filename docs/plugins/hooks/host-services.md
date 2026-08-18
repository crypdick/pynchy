# Host-service hooks

## `pynchy_service_handler`

Provide typed host-side handlers for service tools. Pynchy composes every result
into one immutable host-action catalog; duplicate capability IDs or tool names
fail startup.

```python
@hookimpl
def pynchy_service_handler(self) -> HostActionRegistration:
    return HostActionRegistration(
        actions=(
            HostActionDescriptor(
                capability=CapabilityDescriptor(
                    id=CapabilityId("weather.forecast.read"),
                    kind=CapabilityKind.HOST_ACTION,
                    owner="weather-plugin",
                    summary="Read the current weather forecast.",
                    action_ids=(ActionId("weather.forecast.read"),),
                ),
                tool_name=HostToolName("weather_get_forecast"),
                handler=_handle_forecast,
                access=HostActionAccess.READ,
                approval=ApprovalContract(),
                idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
                audit=AuditContract(),
            ),
        ),
    )
```

`CapabilityDescriptor` owns operator-facing identity, requirements, probes, and
documentation. `HostActionDescriptor` owns dispatch metadata, approval,
idempotency, audit, and optional fallback service trust. `ApprovalContract()`
uses service policy to decide whether an exact request needs approval. Use
`ApprovalMode.SESSION_TOOL` only for a multi-step workflow that should reuse
approval until the session ends. `ApprovalTrigger.CAPABILITY_ONLY` is reserved
for bounded workspace-local state; explicit capability policy and service
prohibitions still apply. `ApprovalTrigger.ALWAYS` makes approval unconditional.

The handler runs in the host process through IPC, not inside an agent container.
Pynchy rechecks capability policy and tool trust at dispatch. Plugins must
return `HostActionRegistration`; startup rejects raw handler mappings.

For each new semantic action, also implement [`pynchy_action_specs`](#pynchy_action_specs).

## `pynchy_computer_use_backend`

Provide one platform implementation for the backend-neutral `computer_use`
action. Pynchy uses the exact provider named in
`[plugins.computer-use.options].provider`. It reports an unavailable provider
instead of silently selecting another implementation.

```python
class MyDesktopPlugin:
    @hookimpl
    def pynchy_computer_use_backend(self) -> MyDesktopBackend:
        return MyDesktopBackend()
```

Provider names must be unique. Implementations receive a validated,
closed `ComputerUseRequest`; translate it to an allowlisted native API or
subprocess invocation. The neutral service keeps policy, audit, idempotency,
attribution, and screenshot ownership. See
[Computer use](../../usage/host-capabilities/computer-use.md).

## `pynchy_action_specs`

Provide semantic action contracts owned by the plugin:

```python
@hookimpl
def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
    return (
        ActionSpec(
            id=ActionId("weather.forecast.read"),
            owner="weather-plugin",
            summary="Read the current weather forecast.",
            surfaces=(ActionSurface(transport=ActionTransport.AGENT_TOOL, name="weather_get_forecast"),),
        ),
    )
```

Built-in and plugin-owned specifications validate together; duplicate or invalid
IDs fail startup. Provider-changing actions should request agentic evidence,
name a canary scenario, and run hermetic action coverage in CI. See [Action
coverage](../../architecture/action-coverage.md).
