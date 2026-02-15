# Style Guide

## Documentation Philosophy

Write documentation from the **user's perspective and goal**, not chronological order. The user is trying to achieve something—help them achieve it by disclosing information when it makes sense in their pursuit of that goal.

**Bad (chronological):** "First we added X, then we refactored Y, then we discovered Z needed changing..."

**Good (goal-oriented):** "To accomplish [goal], do [steps]. Note: [context when relevant to the task]."

Structure documentation around:
- What the user is trying to do
- What they need to know to do it
- Relevant context disclosed at the point of need
- Not the history of how the code evolved

## Code Comments: Capture User Reasoning

When the user gives an instruction or makes a design decision **and explains their reasoning**, capture that reasoning as a comment in the code — right where the decision is implemented. Future maintainers should be able to understand the intent without leaving the code context.

- Only add comments when the user provides a *reason*, not for every instruction
- Place the comment at the point of implementation, not in a separate doc
- Preserve the user's reasoning faithfully — don't paraphrase away the nuance
