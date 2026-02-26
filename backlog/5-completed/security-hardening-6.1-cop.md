Implement the Cop Agent for prompt injection detection.

## Scope

The Cop Agent is a background agent that scans untrusted content for prompt injection attempts before the orchestrator agent sees it. This is NOT a primary defense - it's defense-in-depth alongside the action gating in Step 6.

## Dependencies

- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ Step 2: MCP Tools & Basic Policy (must be complete)
- ✅ Step 6: Human Approval Gate (must be complete - this is the PRIMARY defense)


This cop agent will also be used to scan other untrusted content, such as 3rd party plugins. Each task will have a fresh session.
