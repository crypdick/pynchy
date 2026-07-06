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

## Plan
TBD — likely start with the highest-count Protocol violations (IpcDeps,
WorkspaceProfile, MessageHandlerDeps) since fixing the fake/mock shape once
per Protocol clears hundreds of warnings at a time.
