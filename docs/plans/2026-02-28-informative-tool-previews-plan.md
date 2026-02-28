# Informative Tool Previews Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show mini diffs for Edit and content previews for Write in channel trace messages.

**Architecture:** Enhance `format_tool_preview()` to extract `old_string`/`new_string` (Edit) and `content` (Write) from `tool_input`, format them as prefixed line snippets, and return a multi-line string. Add a shared helper for line truncation.

**Tech Stack:** Pure Python, no new dependencies.

---

### Task 1: Add helper function `_format_lines`

A shared helper that takes a list of lines, a prefix string, and a max-lines count, and returns the formatted snippet. Both Edit and Write will use it.

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/formatter.py:72` (add helper before `format_tool_preview`)
- Test: `tests/test_messaging_formatter.py`

**Step 1: Write the failing tests**

Add to `tests/test_messaging_formatter.py`:

```python
from pynchy.host.orchestrator.messaging.formatter import _format_lines


class TestFormatLines:
    """Test the line-formatting helper used by Edit/Write previews."""

    def test_single_line(self):
        result = _format_lines(["hello"], prefix="-")
        assert result == "- hello"

    def test_multiple_lines(self):
        result = _format_lines(["a", "b", "c"], prefix="+")
        assert result == "+ a\n+ b\n+ c"

    def test_truncates_at_max_lines(self):
        lines = [f"line{i}" for i in range(10)]
        result = _format_lines(lines, prefix="-", max_lines=5)
        assert result.count("\n") == 5  # 5 lines + summary
        assert "(+5 more lines)" in result

    def test_truncates_long_individual_lines(self):
        long_line = "x" * 200
        result = _format_lines([long_line], prefix="+", max_chars=120)
        # prefix "+ " = 2 chars, so content truncated to 118 + "..."
        assert len(result.split("\n")[0]) <= 124  # "+ " + 120 + "..."
        assert result.endswith("...")

    def test_empty_lines_preserved(self):
        result = _format_lines(["a", "", "b"], prefix="+")
        assert result == "+ a\n+ \n+ b"

    def test_empty_input(self):
        result = _format_lines([], prefix="-")
        assert result == ""

    def test_exactly_max_lines_no_summary(self):
        lines = ["a", "b", "c", "d", "e"]
        result = _format_lines(lines, prefix="-", max_lines=5)
        assert "(+0 more lines)" not in result
        assert "more lines" not in result
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_messaging_formatter.py::TestFormatLines -v`
Expected: FAIL with `ImportError` (function doesn't exist yet)

**Step 3: Write minimal implementation**

Add to `formatter.py` before `format_tool_preview`:

```python
def _format_lines(
    lines: list[str],
    *,
    prefix: str,
    max_lines: int = 5,
    max_chars: int = 120,
) -> str:
    """Format lines with a prefix, truncating long lines and excess line count.

    Used by Edit/Write previews to show content snippets in channel messages.
    """
    if not lines:
        return ""
    shown = lines[:max_lines]
    remainder = len(lines) - max_lines
    result_lines = []
    for line in shown:
        if len(line) > max_chars:
            line = line[:max_chars] + "..."
        result_lines.append(f"{prefix} {line}")
    if remainder > 0:
        result_lines.append(f"(+{remainder} more lines)")
    return "\n".join(result_lines)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_messaging_formatter.py::TestFormatLines -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/formatter.py tests/test_messaging_formatter.py
git commit -m "feat: add _format_lines helper for tool preview snippets"
```

---

### Task 2: Update Edit preview to show diff

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/formatter.py:86-92` (Edit branch)
- Test: `tests/test_messaging_formatter.py`

**Step 1: Write the failing tests**

Update the existing Edit test and add new ones in `TestFormatToolPreview`:

```python
    # --- Edit (with diff) ---
    def test_edit_shows_diff(self):
        result = format_tool_preview("Edit", {
            "file_path": "/src/config.py",
            "old_string": "return None",
            "new_string": "return 42",
        })
        assert "Edit: /src/config.py" in result
        assert "- return None" in result
        assert "+ return 42" in result

    def test_edit_multiline_diff(self):
        result = format_tool_preview("Edit", {
            "file_path": "/src/app.py",
            "old_string": "def foo():\n    pass",
            "new_string": "def foo():\n    return 1",
        })
        assert "- def foo():" in result
        assert "-     pass" in result
        assert "+ def foo():" in result
        assert "+     return 1" in result

    def test_edit_truncates_long_diff(self):
        old = "\n".join(f"old_line_{i}" for i in range(10))
        new = "\n".join(f"new_line_{i}" for i in range(10))
        result = format_tool_preview("Edit", {
            "file_path": "/src/big.py",
            "old_string": old,
            "new_string": new,
        })
        assert "(+5 more lines)" in result

    def test_edit_without_old_new_falls_back_to_path(self):
        result = format_tool_preview("Edit", {"file_path": "/src/config.py"})
        assert result == "Edit: /src/config.py"

    def test_edit_missing_path(self):
        result = format_tool_preview("Edit", {})
        assert result == "Edit"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_messaging_formatter.py::TestFormatToolPreview::test_edit_shows_diff -v`
Expected: FAIL (current impl doesn't include diff lines)

**Step 3: Write minimal implementation**

Replace the `if tool_name in ("Read", "Edit", "Write"):` block in `format_tool_preview` with:

```python
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if path:
            if len(path) > 150:
                path = "..." + path[-147:]
            return f"Read: {path}"
        return "Read"

    if tool_name == "Edit":
        path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if not path:
            return "Edit"
        if len(path) > 150:
            path = "..." + path[-147:]
        header = f"Edit: {path}"
        if not old and not new:
            return header
        parts = [header]
        if old:
            parts.append(_format_lines(old.splitlines(), prefix="-"))
        if new:
            parts.append(_format_lines(new.splitlines(), prefix="+"))
        return "\n".join(parts)

    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        if not path:
            return "Write"
        if len(path) > 150:
            path = "..." + path[-147:]
        header = f"Write: {path}"
        if not content:
            return header
        lines = content.splitlines()
        parts = [header]
        parts.append(_format_lines(lines, prefix="+"))
        return "\n".join(parts)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_messaging_formatter.py::TestFormatToolPreview -v`
Expected: PASS (all Edit tests including new ones, plus Read tests still pass)

**Step 5: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/formatter.py tests/test_messaging_formatter.py
git commit -m "feat: show mini diff in Edit tool preview"
```

---

### Task 3: Update Write preview to show content

**Files:**
- Modify: `tests/test_messaging_formatter.py` (Write tests)

Note: The Write implementation was already added in Task 2. This task is just about writing the tests for it.

**Step 1: Write the tests**

Update existing Write tests and add new ones:

```python
    # --- Write (with content preview) ---
    def test_write_shows_content(self):
        result = format_tool_preview("Write", {
            "file_path": "/tmp/out.txt",
            "content": "hello world\nsecond line",
        })
        assert "Write: /tmp/out.txt" in result
        assert "+ hello world" in result
        assert "+ second line" in result

    def test_write_truncates_long_content(self):
        content = "\n".join(f"line_{i}" for i in range(20))
        result = format_tool_preview("Write", {
            "file_path": "/tmp/big.txt",
            "content": content,
        })
        assert "(+15 more lines)" in result

    def test_write_without_content_shows_path_only(self):
        result = format_tool_preview("Write", {"file_path": "/tmp/out.txt"})
        assert result == "Write: /tmp/out.txt"

    def test_write_missing_path(self):
        result = format_tool_preview("Write", {})
        assert result == "Write"
```

**Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_messaging_formatter.py::TestFormatToolPreview -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_messaging_formatter.py
git commit -m "test: add Write content preview tests"
```

---

### Task 4: Run full test suite and lint

**Step 1: Run all formatter tests**

Run: `uv run pytest tests/test_messaging_formatter.py -v`
Expected: All PASS

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: All PASS

**Step 3: Run linter**

Run: `uv run ruff check src/pynchy/host/orchestrator/messaging/formatter.py`
Expected: No errors

**Step 4: Commit any fixes if needed**

---

### Task 5: Deploy

**Step 1: Push changes**

```bash
git push
```

**Step 2: Deploy**

Call `deploy_changes` (no container rebuild needed — this is host-side code only).

**Step 3: Verify**

After deploy, trigger the agent to make an edit and confirm the trace message shows the diff in chat.
