"""Split long agent output into Discord-sized chunks.

Discord rejects messages over 2000 characters. Naively slicing at 2000 would
cut code fences in half, so this splitter tracks fence state line-by-line: when
a chunk boundary falls inside a ```` ``` ```` block it closes the fence on the
current chunk and reopens it on the next, keeping every chunk independently
renderable.

Python strings are code-point sequences (no UTF-16 surrogate pairs), so no
surrogate-safe cutting is needed.
"""

from __future__ import annotations

DISCORD_LIMIT = 2000
_FENCE = "```"
# Room to append "\n```" when a chunk has to close an open fence.
_FENCE_RESERVE = len(_FENCE) + 1


def _is_fence_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def _hard_split(segment: str, max_size: int) -> list[str]:
    """Split one oversized segment into ``<= max_size`` pieces.

    Prefers to break at the last whitespace inside the window; falls back to a
    hard cut. Pieces concatenate back to the input segment exactly.
    """
    pieces: list[str] = []
    remaining = segment
    while len(remaining) > max_size:
        window = remaining[:max_size]
        cut = window.rfind(" ")
        if cut <= 0:
            cut = window.rfind("\n")
        cut = max_size if cut <= 0 else cut + 1  # keep the break char with the left piece
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_discord_text(text: str, *, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split ``text`` into chunks that each fit within ``limit`` characters.

    Plain text reassembles exactly (``"".join(chunks) == text``); fenced code
    blocks are closed and reopened across boundaries, so those chunks carry
    injected fence lines and do not reassemble verbatim.
    """
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    cur_parts: list[str] = []
    cur_len = 0
    in_fence = False
    pending_reopen = False

    def flush() -> None:
        nonlocal cur_parts, cur_len, pending_reopen
        if not cur_parts:
            return
        out = "".join(cur_parts)
        if in_fence:
            if not out.endswith("\n"):
                out += "\n"
            out += _FENCE
        chunks.append(out)
        cur_parts = []
        cur_len = 0
        pending_reopen = in_fence  # next chunk must reopen the fence

    for line in text.splitlines(keepends=True):
        max_size = limit - (_FENCE_RESERVE if in_fence else 0)
        pieces = _hard_split(line, max_size) if len(line) > max_size else [line]
        for piece in pieces:
            budget = limit - (_FENCE_RESERVE if in_fence else 0)
            if cur_parts and cur_len + len(piece) > budget:
                flush()
            if not cur_parts and pending_reopen:
                cur_parts.append(_FENCE + "\n")
                cur_len += len(_FENCE) + 1
                pending_reopen = False
            cur_parts.append(piece)
            cur_len += len(piece)
            if _is_fence_line(piece):
                in_fence = not in_fence

    flush()
    return chunks
