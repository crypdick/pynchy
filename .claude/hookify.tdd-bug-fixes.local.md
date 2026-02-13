---
name: tdd-bug-fixes
enabled: true
event: prompt
conditions:
  - field: user_prompt
    operator: regex_match
    pattern: \b(fix|bug|broken|doesn't work|didn't work|not working|wrong|issue|failing|failed|crash|error)\b
action: warn
---

**Consider TDD for this bug fix.**

Before editing source code to fix this bug:

1. **Write a failing test first** that reproduces the undesirable behavior
2. **Run the test** to confirm it fails for the right reason
3. **Then fix the code** and verify the test passes

This ensures the bug is properly captured and won't regress. Skip TDD only for trivial fixes (typos, obvious one-liners).
