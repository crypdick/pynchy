# Development

Run commands directly—don't tell the user to run them.

## Am I on pynchy?

Check `hostname`. If it returns `pyncher-server`, you're on the server and can access services at `localhost`. Otherwise, reach pynchy over Tailscale (e.g., `ssh pyncher-server`).

```bash
uv run pynchy            # Run the app
uv run pytest tests/     # Run tests
uv run ruff check --fix src/ container/agent_runner/src/  # Lint + autofix
uv run ruff format src/ container/agent_runner/src/       # Format
uvx pre-commit run --all-files  # Run all pre-commit hooks
./container/build.sh     # Rebuild agent container
```

## Documentation Lookup

When you need documentation for a library or framework, use the context7 MCP server to get up-to-date docs. Don't rely on training data for API details that may have changed.

## Testing Philosophy

Write tests that validate **actual business logic**, not just line coverage.

### Good Tests (Real Value)
✅ Test functions with complex branching logic (multiple if/else paths)
✅ Test critical user-facing behavior (message parsing, context resets, formatting)
✅ Test edge cases that could cause bugs (empty inputs, None values, truncation)
✅ Test error conditions and how they're handled
✅ Test data transformations and validation logic
✅ Use descriptive test names that explain what's being validated

### Coverage Theater (Avoid)
❌ Testing trivial getters/setters with no logic
❌ Testing framework-provided functionality (e.g., dataclass equality)
❌ Writing tests just to hit a coverage percentage
❌ Mocking everything so heavily that you're testing the mocks, not real code
❌ Testing implementation details instead of behavior
❌ Tests that would pass even if the code were completely broken

### Examples

**Good:** Testing `is_context_reset()` because:
- Complex logic with multiple valid patterns to match
- Critical business logic (wrong behavior = data loss)
- Many edge cases (case sensitivity, word boundaries, aliases)
- Easy to break with small changes

**Good:** Testing `format_tool_preview()` because:
- Complex branching (different logic per tool type)
- Critical for UX (users need to see what agent is doing)
- Has truncation logic that needs validation
- Many edge cases (None values, special chars, long inputs)

**Coverage Theater:** Testing a simple property accessor:
```python
def test_get_name(self):
    obj = MyClass(name="test")
    assert obj.name == "test"  # Just testing the language works
```

When improving test coverage, focus on **under-tested files with actual logic**:
- Functions with >10 lines and multiple branches
- User-facing features (routing, formatting, triggers)
- Error-prone areas (parsing, validation, state management)
- Code that has caused bugs in the past
