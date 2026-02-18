# Pytest Hang Diagnostics

When pytest reaches 100% pass but the process never exits, it's almost always a
non-daemon thread blocking `threading._shutdown()` — typically an aiosqlite
worker thread whose event loop was destroyed before `close()` was called.

## Root cause in pynchy

`asyncio_mode = "auto"` gives each test its own event loop. When test N+1 calls
`_init_test_database()` → `await _db.close()`, the connection was created on test
N's (now-dead) loop. `aiosqlite` uses `loop.call_soon_threadsafe()` to deliver
the close result; that callback targets the dead loop and never fires → hang.

`-n auto` (xdist) masks this because workers are subprocesses that get killed.
The hang only appears when running without xdist (single test file, `-n 0`,
or `PYTHONTRACEMALLOC` which adds enough overhead to widen the race).

**Fix**: `asyncio_default_fixture_loop_scope = "session"` in pyproject.toml
so all tests share one event loop.

---

## Step 1 — Surface the ResourceWarning

Run with all warnings visible. An unclosed event loop or aiosqlite connection
will emit a `ResourceWarning` after the test summary line:

```bash
uv run pytest -n 0 --timeout=10 -q -W all --override-ini="addopts=" 2>&1 | tail -15
```

Look for:
```
ResourceWarning: unclosed event loop <_UnixSelectorEventLoop running=False closed=False ...>
```

If present, something created an event loop and never closed it. Enable
tracemalloc to find the allocation site (warning: this may itself trigger the
hang — use `timeout`):

```bash
timeout 30 env PYTHONTRACEMALLOC=20 uv run pytest -n 0 --timeout=10 -q -W all --override-ini="addopts=" 2>&1
```

## Step 2 — Find zombie pytest processes

Previous Claude Code sessions may have left hanging pytest processes. These
consume memory and hold file locks:

```bash
ps aux | grep -i pytest | grep -v grep
```

Check thread count per process — a healthy exited pytest has 1 thread. Leaked
aiosqlite connections show as extra non-daemon threads:

```bash
for pid in $(pgrep -f pytest); do
  tc=$(ls /proc/$pid/task/ 2>/dev/null | wc -l)
  cmd=$(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ' | cut -c1-80)
  echo "PID $pid: $tc threads | $cmd"
done
```

**22+ threads = 1 main + N leaked aiosqlite worker threads.**

Kill them:

```bash
pkill -9 -f "pytest.*pynchy"
```

## Step 3 — Reproduce the aiosqlite cross-loop hang

Minimal reproduction that confirms aiosqlite `close()` hangs when the creating
loop is dead:

```bash
timeout 10 uv run python -c "
import asyncio, aiosqlite

_db = None

async def init():
    global _db
    if _db is not None:
        await _db.close()  # hangs: _db was created on the previous (dead) loop
    _db = await aiosqlite.connect(':memory:')

asyncio.run(init())  # loop A: create connection, then loop A is destroyed
asyncio.run(init())  # loop B: close() targets dead loop A → hang
" 2>&1; echo "EXIT=\$?"
```

`EXIT=124` (killed by timeout) confirms the bug.

## Step 4 — Count leaked threads programmatically

Instrument `_init_test_database` to track thread accumulation:

```bash
uv run python -c "
import asyncio, threading

async def test_thread_leak():
    from pynchy.db import _init_test_database
    import pynchy.db._connection as db_conn

    print(f'Before: {threading.active_count()} threads')

    for i in range(5):
        await _init_test_database()
        non_main = [t for t in threading.enumerate() if t.name != 'MainThread']
        print(f'After init #{i+1}: {threading.active_count()} threads')
        for t in non_main:
            print(f'  {t.name} daemon={t.daemon} alive={t.is_alive()}')

    if db_conn._db is not None:
        await db_conn._db.close()
        db_conn._db = None

    import time; time.sleep(0.5)
    print(f'After close: {threading.active_count()} threads')

asyncio.run(test_thread_leak())
print(f'After asyncio.run: {threading.active_count()} threads')
for t in threading.enumerate():
    if t.name != 'MainThread':
        print(f'  {t.name} daemon={t.daemon} alive={t.is_alive()}')
"
```

Inside a single `asyncio.run()`, close works (same loop). The bug only triggers
when close crosses loop boundaries (separate `asyncio.run()` calls, or
pytest-asyncio function-scoped loops).

## Step 5 — Simulate the pytest-asyncio loop lifecycle

Each `asyncio.run()` call mimics pytest-asyncio's per-test event loop. This
is the reproduction that actually hangs:

```bash
timeout 15 uv run python -c "
import asyncio, threading

async def init_db():
    from pynchy.db._connection import _init_test_database
    await _init_test_database()

# Each asyncio.run() = separate event loop, like pytest-asyncio function scope
for i in range(3):
    asyncio.run(init_db())
    print(f'Iteration {i+1}: {threading.active_count()} threads')
"
```

If this hangs (EXIT=124), the cross-loop close is confirmed.

## Step 6 — Verify the fix

After adding `asyncio_default_fixture_loop_scope = "session"` to pyproject.toml,
run without xdist and confirm clean exit:

```bash
timeout 60 uv run pytest -n 0 --timeout=10 -q -W all --override-ini="addopts=" 2>&1
# Should complete with no ResourceWarning and EXIT=0
```

Then run with xdist to confirm no regressions:

```bash
uv run pytest --timeout=10 -q 2>&1
```

Note: session-scoped loops mean `asyncio.StreamReader()` objects created in
fixtures must be inside `async` fixtures (not sync `__init__`). Watch for
`got Future attached to a different loop` errors — these indicate a fixture
creating async primitives outside the running loop.

## Quick reference

| Symptom | Likely cause | Diagnostic |
|---------|-------------|------------|
| 100% pass then hang | Leaked non-daemon thread | `ps` + thread count per PID |
| `ResourceWarning: unclosed event loop` | Orphan loop from import-time code | `-W all` flag |
| 22+ threads in hanging process | aiosqlite cross-loop close bug | Thread enumeration script |
| `Future attached to a different loop` | StreamReader created outside async context | Make fixture `async` |
| Hang only without xdist (`-n 0`) | xdist masks cleanup bugs via process kill | Compare `-n auto` vs `-n 0` |
