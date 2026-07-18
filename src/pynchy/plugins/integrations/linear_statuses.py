"""Standard Pynchy todo statuses for Linear."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TodoStatusSpec:
    name: str
    type: str
    position: float
    color: str


LINEAR_TODO_STATUSES: dict[str, TodoStatusSpec] = {
    "backlog": TodoStatusSpec("Backlog", "backlog", 10.0, "#8A8F98"),
    "planning": TodoStatusSpec("Planning", "unstarted", 20.0, "#F2C94C"),
    "ready": TodoStatusSpec("Ready", "unstarted", 30.0, "#56CCF2"),
    "in_progress": TodoStatusSpec("In Progress", "started", 40.0, "#2F80ED"),
    "blocked": TodoStatusSpec("Blocked", "started", 50.0, "#EB5757"),
    "done": TodoStatusSpec("Done", "completed", 60.0, "#27AE60"),
}
