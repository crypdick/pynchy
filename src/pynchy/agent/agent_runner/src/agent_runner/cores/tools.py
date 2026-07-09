"""Single source of truth for the Claude tool roster shared by both Claude cores.

The SDK core (cores/claude.py, via ``ClaudeAgentOptions``) and the claude-cli
core (cores/claude_cli.py, via ``--allowedTools``/``--disallowedTools``) must
offer the agent the *same* menu of built-in tools, or behaviour silently diverges
by which core is selected. Both cores import these constants instead of
hand-maintaining parallel lists ("parity by construction, not by comment") --
the same pattern used for the security gate in hooks.before_tool_use_roster.

Per-server MCP wildcards (``mcp__<server>__*``) are appended by each core at
runtime from its live config, so they are intentionally *not* listed here.
"""

from __future__ import annotations

# Built-in tools the agent may use. MCP wildcards are added per-core at runtime.
#
# NOTE: native Teams tools (TeamCreate/TeamDelete/SendMessage) are deliberately
# absent. pynchy runs one agent per group turn against a single shared session
# JSONL; teammate processes could branch that transcript mid-turn, and pynchy
# resumes by bare session-id with no UUID anchor (the TS port's resumeSessionAt
# was lost, with no CLI successor), so a stale branch tip could be selected on
# the next resume. Agent->human messaging uses the namespaced
# mcp__pynchy__send_message tool, not native SendMessage. Teams can't be
# supported safely until teammates get real per-teammate session isolation.
BUILTIN_ALLOWED_TOOLS: list[str] = [
    "Bash",
    "BashOutput",
    "KillBash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "Task",
    "TaskOutput",
    "TaskStop",
    "TodoWrite",
    "ToolSearch",
    "Skill",
    "NotebookEdit",
    "mcp__pynchy__*",
]

# Plan-mode / interactive tools that would hang a headless container: they block
# on interactive approval that never arrives.
DISALLOWED_TOOLS: list[str] = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]
