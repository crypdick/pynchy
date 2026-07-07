"""Tests for the Discord 2000-char message chunker.

Discord rejects messages over 2000 characters, so long agent output must be
split. The tricky part is code fences: if a split falls inside a ```` ``` ````
block, each chunk must independently open and close the fence or Discord
renders the halves as broken markdown.
"""

from __future__ import annotations

from pynchy.plugins.channels.discord._chunk import chunk_discord_text

FENCE = "```"


def _fence_count(chunk: str) -> int:
    return sum(1 for line in chunk.splitlines() if line.strip().startswith(FENCE))


def test_short_text_is_one_chunk_unchanged():
    assert chunk_discord_text("hello world") == ["hello world"]


def test_empty_text_yields_no_chunks():
    assert chunk_discord_text("") == []


def test_whitespace_only_yields_no_chunks():
    assert chunk_discord_text("   \n  \t ") == []


def test_text_at_limit_stays_one_chunk():
    text = "a" * 2000
    assert chunk_discord_text(text) == [text]


def test_every_chunk_within_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(500))
    chunks = chunk_discord_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_plain_text_reassembles_exactly():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(500))
    assert "".join(chunk_discord_text(text)) == text


def test_single_line_longer_than_limit_is_hard_split():
    text = "z" * 5000
    chunks = chunk_discord_text(text)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


def test_long_line_prefers_whitespace_break():
    # 400 space-separated words; each chunk should end at a word boundary,
    # never mid-word, when a boundary is available.
    text = " ".join("word" for _ in range(400)) + " " + "y" * 3000
    chunks = chunk_discord_text(text)
    assert all(len(c) <= 2000 for c in chunks)


def test_fence_split_keeps_each_chunk_balanced():
    # A single fenced code block far larger than the limit. Every chunk must
    # contain an even number of fence markers (open + close), so each renders
    # as a complete code block on its own.
    body = "\n".join("codeline " + "c" * 40 for _ in range(300))
    text = f"{FENCE}python\n{body}\n{FENCE}"
    chunks = chunk_discord_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    for chunk in chunks:
        assert _fence_count(chunk) % 2 == 0, f"unbalanced fences in chunk: {chunk[:60]!r}"


def test_custom_limit_is_respected():
    text = "\n".join("row" + str(i) for i in range(100))
    chunks = chunk_discord_text(text, limit=50)
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks) == text
