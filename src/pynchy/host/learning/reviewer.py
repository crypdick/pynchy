"""Prompting and triage for hidden Obsidian learning review jobs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from pynchy.host.learning.packet_models import LearningPacket
from pynchy.host.learning.paths import LearningPaths

_EXPLICIT_LEARNING_PATTERNS = (
    re.compile(r"\bremember\b", re.IGNORECASE),
    re.compile(r"\blearn this\b", re.IGNORECASE),
    re.compile(r"\bsave this\b", re.IGNORECASE),
    re.compile(r"\bfile this\b", re.IGNORECASE),
    re.compile(r"\bmake a note\b", re.IGNORECASE),
    re.compile(r"\bnote that\b", re.IGNORECASE),
    re.compile(r"\badd this to (?:memory|the vault)\b", re.IGNORECASE),
)
_WORKFLOW_MARKERS = (
    "whenever",
    "when i ask",
    "every time",
    "each time",
    "next time",
    "from now on",
    "going forward",
    "workflow",
    "procedure",
    "playbook",
    "runbook",
)
_WORKFLOW_ACTIONS = (
    "run ",
    "use ",
    "check ",
    "commit",
    "deploy",
    "restart",
    "ship ",
    "open ",
    "write ",
    "create ",
)
_LOW_SIGNAL_WORDS = {
    "awesome",
    "cool",
    "good",
    "got",
    "great",
    "haha",
    "hi",
    "hello",
    "hey",
    "it",
    "lol",
    "nice",
    "no",
    "ok",
    "okay",
    "problem",
    "re",
    "sounds",
    "thank",
    "thanks",
    "thx",
    "welcome",
    "yes",
    "you",
    "yep",
}
_MIN_REVIEW_TEXT_CHARS = 240


def should_review(packet: LearningPacket) -> bool:
    """Return whether a packet is worth sending to the hidden learning reviewer."""
    text = _packet_text(packet)
    normalized = _normalize_text(text)
    if not normalized:
        return False

    if _contains_explicit_learning_signal(text):
        return True
    if packet.error_snippets:
        return True
    if _looks_like_repeatable_workflow(normalized):
        return True
    if packet.tool_counts:
        return True
    if packet.loaded_skills and len(normalized) >= _MIN_REVIEW_TEXT_CHARS // 2:
        return True

    if _is_short_low_signal_turn(normalized):
        return False
    return len(normalized) >= _MIN_REVIEW_TEXT_CHARS


def build_review_prompt(packet: LearningPacket, paths: LearningPaths) -> str:
    """Build the hidden reviewer instruction prompt for one learning packet."""
    packet_payload = json.dumps(asdict(packet), ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "You are Pynchy's hidden learning reviewer. Inspect the captured "
        "turn and update the mounted Obsidian vault only when the "
        "conversation contains durable, factual learning.\n\n"
        "Vault namespace and placement policy:\n"
        "- The mounted vault root is the global memory namespace.\n"
        f"- Mounted vault root: {paths.vault_mount_path}\n"
        "- Use existing folder organization first.\n"
        "- Use the profile fallback memory path only when no repo, machine, "
        "subject, or other existing folder clearly fits.\n"
        f"- Profile fallback memory path: {paths.mounted_memory_root}\n"
        "- Keep notes small and factual; update existing notes when that is "
        "cleaner than adding new ones.\n"
        "- If nothing durable was learned, make no filesystem changes.\n\n"
        "Memory note policy:\n"
        "- Memory notes are folder-governed; they should not depend on "
        "semantic frontmatter.\n"
        "- Do not invent semantic frontmatter requirements for memory notes.\n"
        "- Prefer a concise note in the strongest existing semantic folder "
        "over a broad catch-all note.\n\n"
        "Learned skill policy:\n"
        "- Write learned skills only under the profile skill path.\n"
        f"- Profile skill path: {paths.mounted_skills_root}\n"
        "- Learned skills belong under the profile skill namespace and must "
        "use Pynchy's existing `SKILL.md` skill format.\n"
        "- Create or update a learned skill only for repeatable workflows, "
        "not one-off facts.\n\n"
        "Review the packet below. Make the smallest filesystem changes that "
        "preserve durable learning.\n\n"
        "```json\n"
        f"{packet_payload}\n"
        "```\n"
    )


def _packet_text(packet: LearningPacket) -> str:
    parts: list[str] = []
    for message in packet.messages:
        content = message.get("content")
        if content:
            parts.append(content)
    if packet.final_answer:
        parts.append(packet.final_answer)
    parts.extend(packet.error_snippets)
    parts.extend(packet.loaded_skills)
    return "\n".join(parts)


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def _contains_explicit_learning_signal(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _EXPLICIT_LEARNING_PATTERNS)


def _looks_like_repeatable_workflow(normalized: str) -> bool:
    return any(marker in normalized for marker in _WORKFLOW_MARKERS) and any(
        action in normalized for action in _WORKFLOW_ACTIONS
    )


def _is_short_low_signal_turn(normalized: str) -> bool:
    if len(normalized) > 120:
        return False
    words = set(re.findall(r"[a-z']+", normalized))
    return bool(words) and words <= _LOW_SIGNAL_WORDS
