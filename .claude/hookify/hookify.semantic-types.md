---
name: semantic-types
enabled: true
event: file
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.py$
  - field: new_text
    operator: regex_match
    pattern: (user_id|account_id|org_id|slug|token|amount|price|email|url|path)\s*:\s*(str|int|float)\b
action: warn
---

Bare primitive type detected for what looks like a domain concept. Semantic types help both humans and AI agents understand the code:

```python
from typing import NewType

UserId = NewType("UserId", str)
Amount = NewType("Amount", int)

def get_user(user_id: UserId) -> User:  # Clear intent
    ...
```

`NewType` is zero-cost at runtime and catches category errors at type-check time (passing an `OrgId` where a `UserId` is expected).

If this is genuinely a raw primitive with no domain meaning, ignore this message.
