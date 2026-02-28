# Informative Tool Previews for Edit/Write

## Problem

When the agent edits or writes files, the chat trace shows only:
```
🔧 Edit: /path/to/file.py
🔧 Write: /path/to/file.py
```

Users can't see *what* changed without reading the file themselves.

## Design

Enhance `format_tool_preview()` in `formatter.py` to show a mini diff for Edit and a content preview for Write. All other tools unchanged.

### Edit

Show file path on the first line, then up to 5 lines of `old_string` prefixed with `-` and up to 5 lines of `new_string` prefixed with `+`. Lines beyond 5 get a `(+N more lines)` note. Each line truncated at 120 chars.

Example:
```
Edit: /workspace/project/src/formatter.py
- def old_function():
-     return None
+ def new_function():
+     return 42
```

Multi-line example with truncation:
```
Edit: /workspace/project/src/big_module.py
- line1
- line2
- line3
- line4
- line5
(+12 more lines)
+ new_line1
+ new_line2
+ new_line3
+ new_line4
+ new_line5
(+15 more lines)
```

### Write

Show file path on the first line, then up to 5 lines of content prefixed with `+`. Remaining lines summarized.

Example:
```
Write: /workspace/project/src/new_file.py
+ """New module for handling..."""
+
+ import os
+ from pathlib import Path
(+42 more lines)
```

### Unchanged

All other tool previews (Bash, Read, Grep, Glob, WebFetch, WebSearch, Task, AskUserQuestion) remain exactly as-is.

## Scope

- `src/pynchy/host/orchestrator/messaging/formatter.py` — `format_tool_preview()` function
- `tests/test_messaging_formatter.py` — updated tests for Edit and Write

No changes to router, types, IPC, or anything else.

## Truncation Rules

- Max 5 lines shown per section (old_string, new_string, or write content)
- Each line truncated at 120 characters (with `...` suffix)
- Overall preview capped at existing 200-char limit for the first line (path), but diff lines are additional
