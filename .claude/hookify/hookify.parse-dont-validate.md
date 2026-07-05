---
name: parse-dont-validate
enabled: true
event: file
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.py$
  - field: new_text
    operator: regex_match
    pattern: (isinstance\(.*,\s*(str|int|dict|list)\)|def \w+\(.*:\s*dict\b|-> None.*\n.*raise|\.get\(|if .+ is not None)
action: warn
---

Possible validate-then-discard pattern detected. The principle "parse, don't validate" means: coerce unstructured data into constrained types at the boundary of your system, so downstream code never needs to re-validate.

**Instead of validating and discarding the evidence:**
```python
def process(data: dict) -> None:
    if "user_id" not in data:
        raise ValueError("missing user_id")  # checked and discarded
```

**Parse into a constrained type that carries proof:**
```python
@dataclass(frozen=True)
class UserRequest:
    user_id: UserId
    # Construction IS validation. If it exists, it's valid.

def process(request: UserRequest) -> None:
    # No validation needed — the type proves it.
```

Use Pydantic models, frozen dataclasses, or `NewType` to carry proof through the type system. Parse at the boundary, execute with confidence downstream.

The same boundary applies to untyped third-party clients (SDKs, HTTP responses, DB rows): wrap them in a thin typed adapter that parses their loose `dict`/`Any` output into your own constrained types, so the weak types stop at the edge instead of leaking through the codebase.

If this is internal code operating on already-parsed types, ignore this message.
