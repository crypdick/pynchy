#!/usr/bin/env python3
"""Prek hook to enforce file length limits.

Philosophy: Large files are harder to understand, test, and review.  Keeping
files under a logical-line budget encourages modularity and separation of
concerns.

Logical lines of code (LLOC) are lines that are not empty, not comments, and
not part of docstrings or standalone string literals.

Arguments:
  --max-lines N   Maximum allowed logical lines per file (default: 450)

Allowed:
- Files with ``# allow: file-length`` in the first 5 lines

Exit codes:
  0 - All checks passed
  1 - Violations found
"""

import argparse
import ast
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path


class LogicalLineCounter(ast.NodeVisitor):
    """Counts logical lines of code, ignoring docstrings and comments."""

    def __init__(self) -> None:
        self.docstring_lines: set[int] = set()

    def visit_Expr(self, node: ast.Expr) -> None:
        """Identify standalone string literals (pseudo-docstrings)."""
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            start = node.lineno
            end = node.end_lineno if hasattr(node, "end_lineno") else start
            if end is None:
                end = start

            for lineno in range(start, end + 1):
                self.docstring_lines.add(lineno)

        self.generic_visit(node)


def count_logical_lines(filepath: Path) -> int:
    """Count logical lines (non-empty, non-comment, non-docstring) in a file."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return 0

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return 0

    # First pass: identify lines that are part of docstrings/string-literals
    counter = LogicalLineCounter()
    counter.visit(tree)

    lines = content.splitlines()
    logical_count = 0

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()

        # 1. Skip empty lines
        if not stripped:
            continue

        # 2. Skip comments
        if stripped.startswith("#"):
            continue

        # 3. Skip docstrings/string-literals (tracked by AST visitor)
        if i in counter.docstring_lines:
            continue

        logical_count += 1

    return logical_count


def check_file_length(
    filepath: Path,
    max_lines: int,
    baseline: Mapping[str, int] | None = None,
) -> tuple[int, str] | None:
    """Check whether *filepath* exceeds *max_lines* logical lines.

    Returns ``(lloc, message)`` on violation or ``None`` if the file is fine.
    """
    lloc = count_logical_lines(filepath)
    baseline_lines = (baseline or {}).get(filepath.as_posix())
    if baseline_lines is not None and lloc <= max_lines:
        return (
            lloc,
            f"File is within the {max_lines}-logical-line limit; remove its baseline entry",
        )
    if lloc > max_lines:
        if baseline_lines is not None and lloc <= baseline_lines:
            return None
        if baseline_lines is not None:
            return (
                lloc,
                (
                    f"File grew from its {baseline_lines}-logical-line baseline to {lloc} "
                    f"(limit {max_lines}) — split it instead"
                ),
            )
        return (
            lloc,
            (
                f"File has {lloc} logical lines (limit {max_lines}) "
                f"— split into smaller, focused modules "
                f"(e.g., extract helpers, constants, or a sub-package)"
            ),
        )
    return None


def load_baseline(path: Path, max_lines: int) -> dict[str, int]:
    """Load grandfathered file budgets that must shrink, never grow."""
    try:
        entries = tomllib.loads(path.read_text(encoding="utf-8")).get("files", {})
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Could not load file-length baseline {path}: {error}") from error

    if not isinstance(entries, dict) or any(
        not isinstance(filename, str)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= max_lines
        for filename, limit in entries.items()
    ):
        raise ValueError(
            f"File-length baseline {path} must map paths to integer limits above {max_lines}"
        )
    return entries


def main(filenames: list[str] | None = None) -> int:
    """Run file-length check on provided files."""
    parser = argparse.ArgumentParser(
        description="Check for files exceeding logical line count limit"
    )
    parser.add_argument("filenames", nargs="*", help="Filenames to check")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=450,
        help="Maximum allowed logical lines per file (default: 450)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="TOML file of grandfathered path-to-limit entries that may only shrink",
    )
    args = parser.parse_args(filenames)

    try:
        baseline = load_baseline(args.baseline, args.max_lines) if args.baseline else {}
    except ValueError as error:
        parser.error(str(error))

    exit_code = 0
    total_violations = 0

    for filename in args.filenames:
        filepath = Path(filename)

        if filepath.suffix != ".py":
            continue

        # Allow file-level ignore via comment in first 5 lines
        try:
            with filepath.open(encoding="utf-8") as f:
                first_lines = [next(f, "") for _ in range(5)]
                if any("# allow: file-length" in line for line in first_lines):
                    continue
        except OSError:
            continue

        result = check_file_length(filepath, args.max_lines, baseline)

        if result is not None:
            _lloc, message = result
            exit_code = 1
            total_violations += 1
            print(f"{filename}:1: {message}")

    if exit_code != 0:
        print("\n" + "=" * 70)
        print(f"Found {total_violations} file(s) exceeding the line limit.")
        print()
        print("  FIX the code (preferred):")
        print("     - Extract helper functions into a utils module")
        print("     - Move constants/config to a dedicated file")
        print("     - Split large classes into mixins or sub-classes")
        print()
        print("  EXEMPT with '# allow: file-length' in the first 5 lines")
        print("  ONLY when splitting is genuinely unfeasible.")
        print("=" * 70)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
