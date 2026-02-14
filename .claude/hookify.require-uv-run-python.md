---
name: require-uv-run-python
enabled: true
event: bash
conditions:
  - field: command
    operator: regex_match
    pattern: (^|\s|&&|\|\||;)\s*python3?\s
  - field: command
    operator: not_contains
    pattern: uv run
action: block
---

**Use `uv run` instead of bare `python`.**

This project uses uv for dependency management. Running `python` directly bypasses the managed virtualenv.

Instead of:
- `python script.py` → `uv run script.py`
- `python -m pytest` → `uv run pytest`
- `python3 -c "..."` → `uv run python -c "..."`
