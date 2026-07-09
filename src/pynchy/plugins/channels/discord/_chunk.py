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
    return stripped.startswith(("```", "~~~"))


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


def _chunk_budget(limit: int, *, in_fence: bool) -> int:
    return limit - (_FENCE_RESERVE if in_fence else 0)


def _line_pieces(line: str, *, limit: int, in_fence: bool) -> list[str]:
    budget = _chunk_budget(limit, in_fence=in_fence)
    return _hard_split(line, budget) if len(line) > budget else [line]


def _flush_chunk(
    chunks: list[str],
    parts: list[str],
    *,
    in_fence: bool,
) -> bool:
    if not parts:
        return False
    out = "".join(parts)
    if in_fence:
        if not out.endswith("\n"):
            out += "\n"
        out += _FENCE
    chunks.append(out)
    return in_fence


def _append_piece(
    parts: list[str],
    piece: str,
    *,
    cur_len: int,
    pending_reopen: bool,
) -> tuple[int, bool]:
    if not parts and pending_reopen:
        parts.append(_FENCE + "\n")
        cur_len += len(_FENCE) + 1
        pending_reopen = False
    parts.append(piece)
    return cur_len + len(piece), pending_reopen


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

    for line in text.splitlines(keepends=True):
        pieces = _line_pieces(line, limit=limit, in_fence=in_fence)
        for piece in pieces:
            budget = _chunk_budget(limit, in_fence=in_fence)
            if cur_parts and cur_len + len(piece) > budget:
                pending_reopen = _flush_chunk(chunks, cur_parts, in_fence=in_fence)
                cur_parts = []
                cur_len = 0
            cur_len, pending_reopen = _append_piece(
                cur_parts,
                piece,
                cur_len=cur_len,
                pending_reopen=pending_reopen,
            )
            if _is_fence_line(piece):
                in_fence = not in_fence

    _flush_chunk(chunks, cur_parts, in_fence=in_fence)
    return chunks
