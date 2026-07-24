---
name: python-heredoc
description: When running multi-line Python code or code with quotes, apostrophes, or f-strings via Bash, always use heredoc syntax instead of python -c to avoid shell quoting issues.
tier: core
---

# Python Heredoc Pattern

Run Python via `Bash`? **Never** `python -c "..."` beyond trivial one-liners. Shell quoting break on f-strings, apostrophes, nested quotes, escape sequences.

## Use heredoc syntax instead

```bash
uv run python << 'PYTHON_CODE'
import json

data = {"name": "it's working", "value": f"{1 + 2}"}
print(json.dumps(data, indent=2))
PYTHON_CODE
```

Single quotes round `'PYTHON_CODE'` block shell var expansion. `$variables` and backticks stay literal Python.

## With dependencies

```bash
uv run --with requests python << 'PYTHON_CODE'
import requests

resp = requests.get("https://api.example.com/data")
print(resp.json())
PYTHON_CODE
```

## Rules

1. Always `uv run python` (not bare `python` or `python3`)
2. Always quote delimiter: `<< 'PYTHON_CODE'` (not `<< PYTHON_CODE`)
3. Closing `PYTHON_CODE` own line, no leading whitespace
4. Never `python -c` for code with quotes, f-strings, multiple statements
