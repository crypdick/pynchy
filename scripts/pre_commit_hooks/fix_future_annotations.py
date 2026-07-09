#!/usr/bin/env python3
"""Pre-commit hook to fix placement of ``from __future__ import annotations``.

Philosophy: PEP 563 deferred evaluation of annotations enables cleaner type
hints (no forward-reference strings, union syntax with ``|``, etc.).  This
hook ensures the import exists at the canonical location in every Python file
that already has it, and moves it there if it has drifted.

Behaviour:
- Finds ``from __future__ import annotations`` in each file.
- Computes the correct insertion point: after shebang, encoding cookie,
  uv PEP-723 script metadata block, and module docstring.
- If the import is not at that point, removes it and re-inserts it.
- Preserves CRLF vs LF line endings.
- Exits 1 if any file was modified (so pre-commit stops for re-staging).

Exit codes:
  0 - No changes needed
  1 - Files were modified (re-stage and re-commit)
  2 - An error occurred
"""

import sys
from pathlib import Path


def _is_docstring_start(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(('"""', "'''"))


def _docstring_end_idx(lines: list[str], start_idx: int) -> int | None:
    """If ``lines[start_idx]`` begins a triple-quoted string, return the index
    of the line *after* the closing delimiter.  Otherwise return ``None``.
    """
    first = lines[start_idx]
    s = first.lstrip()
    if s.startswith('"""'):
        delim = '"""'
    elif s.startswith("'''"):
        delim = "'''"
    else:
        return None

    # Single-line docstring
    if s.count(delim) >= 2:
        return start_idx + 1

    for i in range(start_idx + 1, len(lines)):
        if delim in lines[i]:
            return i + 1
    return None


def _find_insertion_point(lines: list[str]) -> int:
    """Return the line index where ``from __future__ import annotations``
    should live: after shebang, encoding cookie, an optional uv script
    metadata block, and an optional module docstring.
    """
    i = 0
    # shebang
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    # encoding cookie (PEP 263) can be on 1st or 2nd line
    if i < len(lines) and "coding" in lines[i] and lines[i].lstrip().startswith("#"):
        i += 1
    # blank lines after shebang/encoding
    while i < len(lines) and not lines[i].strip():
        i += 1
    # uv PEP-723 script metadata block
    if i < len(lines) and lines[i].strip() == "# /// script":
        i += 1
        while i < len(lines):
            if lines[i].strip() == "# ///":
                i += 1
                break
            i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    # optional module docstring
    if i < len(lines) and _is_docstring_start(lines[i]):
        end = _docstring_end_idx(lines, i)
        if end is not None:
            i = end
        while i < len(lines) and not lines[i].strip():
            i += 1
    return i


def _detect_newline_style(raw: str) -> str:
    """Detect newline style.  Only preserves CRLF when the file is *pure* CRLF."""
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    return "\r\n" if crlf and lf == 0 else "\n"


def _fix_file(path: Path) -> bool:
    """Move ``from __future__ import annotations`` to the correct location.

    Returns ``True`` if the file was modified.
    """
    raw = path.read_text(encoding="utf-8")
    nl = _detect_newline_style(raw)
    text = raw.replace("\r\n", "\n")
    lines = text.splitlines(keepends=True)

    target = "from __future__ import annotations\n"

    # Find existing import line
    existing_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "from __future__ import annotations":
            existing_idx = i
            break
    if existing_idx is None:
        return False

    # Already at the correct insertion point -- nothing to do
    if existing_idx == _find_insertion_point(lines):
        return False

    # Remove the existing line and re-insert at the right place
    lines.pop(existing_idx)
    insert_at = _find_insertion_point(lines)
    lines.insert(insert_at, target)

    # Ensure a blank line after the import if the next line is non-blank
    if insert_at + 1 < len(lines) and lines[insert_at + 1].strip():
        lines.insert(insert_at + 1, "\n")

    new_text = "".join(lines)
    if nl == "\r\n":
        new_text = new_text.replace("\n", "\r\n")
    if new_text == raw:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _iter_target_files(repo_root: Path, argv: list[str]) -> list[Path]:
    """Resolve target files from CLI arguments or scan the repo."""
    if argv:
        out: list[Path] = []
        for raw in argv:
            p = (repo_root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            try:
                p.relative_to(repo_root)
            except ValueError:
                continue
            out.append(p)
        return out

    # Manual invocation path: scan the repo when no filenames are provided.
    return list(repo_root.rglob("*.py"))


_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "build", "dist", "node_modules"})


def main(argv: list[str]) -> int:
    """Fix future-annotations placement in provided files."""
    repo_root = Path.cwd().resolve()

    changed: list[Path] = []
    for p in _iter_target_files(repo_root, argv):
        if not p.exists() or not p.is_file():
            continue
        if p.suffix != ".py":
            continue
        rel_parts = p.relative_to(repo_root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if p.is_symlink():
            continue
        try:
            if _fix_file(p):
                changed.append(p)
        except (OSError, UnicodeDecodeError) as e:
            print(f"{p}:1: error processing file — {e}")
            return 2

    if changed:
        print(
            f"Moved 'from __future__ import annotations' to the correct "
            f"location in {len(changed)} file(s) — re-stage and re-commit:"
        )
        for p in changed:
            print(f"  {p}")
        # Non-zero so pre-commit stops and user can re-stage
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
