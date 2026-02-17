"""Live integration tests — require real service connections.

These tests are skipped by default. Opt in with:

    uv run pytest tests/live/ -m live

Or run only the channel parity subset:

    uv run pytest tests/live/ -m "live and parity"
"""
