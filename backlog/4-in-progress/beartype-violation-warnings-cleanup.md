# Clean up beartype violation warnings surfaced by re-enablement

## Context

Phase 2 re-enabled `beartype_this_package()` in `src/pynchy/__init__.py`
(configured with `violation_type=UserWarning`, so violations warn rather than
crash). With it active, a full `uv run pytest` run produces ~1500
`UserWarning`s — pre-existing type looseness that was invisible until runtime
checking landed. None of them fail tests (that's the point of
`violation_type=UserWarning`), but they're real signal:

- **~1000+ warnings**: test doubles (`Mock`/`MagicMock`/hand-rolled `Fake*`
  classes) passed where a `@runtime_checkable` Protocol is expected
  (`IpcDeps`, `MessageHandlerDeps`, `Channel`, `OutputDeps`, `BusDeps`, etc.).
  The fakes don't structurally satisfy the real Protocol shape. Fix options:
  spec `Mock(spec=RealProtocol)`, or make hand-written fakes implement the
  full Protocol surface.
- **A few dozen warnings**: genuine `int` passed where `float` is annotated
  (e.g. `ttl_seconds=3600`, `delay=0`), and a couple of sentinel-object params
  typed too narrowly (`asyncio.subprocess.Process | None` fed a plain
  `object()` in a test).

## Progress

Down from ~1524 to 199 warnings (87% reduction). Full suite green, all
pre-commit hooks (ruff, mypy strict, tests) pass at each step. Key patterns
established:

- **Protocol test doubles** (`IpcDeps`, `Channel`, `MessageHandlerDeps`,
  `OutputDeps`, `WorkspaceProfile`): added `NullIpcDeps` / `NullChannel`
  no-op base classes to `tests/conftest.py` — hand-rolled fakes subclass
  and override only what they exercise. For `MagicMock()` stand-ins, added
  `spec=Protocol`.
  - **Gotcha**: a bare `MagicMock()` can fail `isinstance()` against a
    runtime_checkable Protocol even when every required attribute is
    individually present via `hasattr()` — `abc`'s `__instancecheck__`
    negative-cache is keyed by `type(instance)`, so an earlier check against
    a less-populated Mock poisons all later checks for that Protocol/type
    pair. `spec=Protocol` sidesteps this by satisfying isinstance via the
    fast `issubclass(instance.__class__, cls)` path instead of structural
    hasattr checks.
  - **Gotcha**: `spec=` on a *class* (not instance) only knows attributes
    visible via `dir(cls)` — this breaks for Pydantic `Settings`, whose
    fields aren't class-level attributes. Don't `spec=Settings`; use the
    real `make_settings()` factory from conftest instead.
- **Concrete classes** (`asyncio.subprocess.Process`, `pluggy.PluginManager`):
  hand-rolled fakes with real custom behavior (not simple Mocks) can just
  add the real class as a base and define their own `__init__` that never
  calls `super().__init__()` — `isinstance()` only checks the class
  hierarchy, not that `__init__` ran. Cheap, no behavior change.
- **Narrow production types**: a few were genuine bugs, not test issues —
  `TtlCache.__init__(ttl_seconds: float = 3600)` and
  `run_shell_command(timeout_seconds: float = 600)` had int defaults/literals
  that violated their own float hints. Fixed the default (`3600.0`) or
  widened the hint to `int | float` where callers legitimately pass whole-second
  ints (`ContainerConfig.timeout`, `TaskRunLog.duration_ms` — the latter
  because SQLite's `duration_ms INTEGER` column round-trips as `int`).
- **Known non-bug**: `test_result_with_dict_result_serialized_to_json` in
  `test_messaging_router.py` deliberately constructs an invalid
  `ContainerOutput` to test defensive JSON-serialization of malformed
  container output — that warning is expected signal, not a fake to fix.

## Remaining (199 warnings, by category)

- `Settings` (45) — mostly in `test_message_handler.py`'s `_settings_mock`/
  `_loop_settings_mock`; blocked on the `spec=` class-vs-instance gotcha
  above, needs a real `make_settings()`-based rework instead of `MagicMock`.
- `subprocess.CompletedProcess[str]` (35) — likely the same `spec=` pattern
  as `asyncio.subprocess.Process`.
- `BusDeps` (18) — same Protocol-fake pattern as `IpcDeps`/`Channel`.
- `LiteLLMGateway | BuiltinGateway | None` (19 combined) — gateway union type.
- `PluginManager` variants (~21 combined) — same fix as applied in
  `test_container_runner.py` (real subclass + no-op `__init__`), needs
  repeating across `test_channel_runtime.py`, `test_git_sync.py`,
  `test_http_server.py`, `test_trust_config.py`, `test_tunnels.py`,
  `test_workspace_config.py`, `test_workspace_reconcile.py`.
- `list[Channel]` (9), `WorktreeNotifyDeps` (8), `str`/`dict`/`bool`/
  `aiohttp.web_request.Request`/`RepoContext` (long tail, ~30 combined).
