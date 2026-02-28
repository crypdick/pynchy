---
name: python-heredoc
description: When running multi-line Python code or code with quotes, apostrophes, or f-strings via Bash, always use heredoc syntax instead of python -c to avoid shell quoting issues.
tier: core
---

# Python Heredoc Pattern

When you need to run Python code via `Bash`, **never** use `python -c "..."` for anything beyond trivial one-liners. Shell quoting breaks with f-strings, apostrophes, nested quotes, and escape sequences.

## Use heredoc syntax instead

```bash
uv run python << 'PYTHON_CODE'
import json

data = {"name": "it's working", "value": f"{1 + 2}"}
print(json.dumps(data, indent=2))
PYTHON_CODE
```

The single quotes around `'PYTHON_CODE'` prevent shell variable expansion, so `$variables` and backticks are treated as literal Python code.

## With dependencies

```bash
uv run --with requests python << 'PYTHON_CODE'
import requests

resp = requests.get("https://api.example.com/data")
print(resp.json())
PYTHON_CODE
```

## Rules

1. Always use `uv run python` (not bare `python` or `python3`)
2. Always quote the delimiter: `<< 'PYTHON_CODE'` (not `<< PYTHON_CODE`)
3. The closing `PYTHON_CODE` must be on its own line with no leading whitespace
4. Never use `python -c` for code containing quotes, f-strings, or multiple statements
