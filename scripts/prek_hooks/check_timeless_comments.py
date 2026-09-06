#!/usr/bin/env python3
"""Prek hook to enforce timeless comments.

Philosophy: Comments should describe *what* code does and *why*, not narrate
repository history. Temporal language that centers chronology instead of
current behavior makes comments age poorly and obscures the code's present
intent.

Detects:
- Temporal keywords in inline comments and docstrings (see ``TEMPORAL_KEYWORDS``)

Allowed:
- Lines annotated with ``# allow: timeless-comments``
- Lines containing ``# temporal-ok``
- Lines containing the hourglass emoji (U+23F3)
- Lines with ``TODO`` or ``FIXME`` (inherently time-bound by nature)

Exit codes:
  0 - All checks passed
  1 - Violations found
"""

import argparse
import re
import sys
from pathlib import Path

# Keywords that indicate temporal language in comments
TEMPORAL_KEYWORDS = [
    r"\blegacy\b",
    r"\bfallback\b",
    r"\bold\b",
    r"\bobsolete\b",
    r"\bhas been\b",
    r"\bused to\b",
    r"\bis being\b",
    r"\bnew\b",
    r"\bprevious\b",
    r"\bprior\b",
    r"\bdeprecate[ds]?\b",
    r"\boriginal\b",
    r"\brefactor\b",
    r"\breplace[ds]?\b",
    r"\bmigrate[ds]?\b",
    r"\bupgrade[ds]?\b",
    r"\bno longer\b",
    r"\bunused\b",
    r"\bhistoric\b",
    r"\bremoved\b",
    r"\bswitch\b",
    r"\bcompatibility\b",
    r"\bcompatible\b",
    r"\bformer\b",
    r"\bis now\b",
    r"\bwere removed\b",
    r"\bfor now\b",
    r"\bin favor of\b",
    r"\bbut wait\b",
    r"\bactually,\b",
    r"\bwait,\b",
    r"\bah!\b",
]


def extract_comments(file_path: Path) -> list[tuple[int, str]]:
    """Extract all comments from a Python file.

    Returns list of (line_number, comment_text) tuples.
    """
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return []

    comments = []
    in_docstring = False
    docstring_delimiter: str | None = None

    for line_num, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Handle docstrings
        if in_docstring:
            comments.append((line_num, line))
            if docstring_delimiter is not None and docstring_delimiter in line:
                in_docstring = False
        elif stripped.startswith(('"""', "'''")):
            docstring_delimiter = '"""' if stripped.startswith('"""') else "'''"
            in_docstring = True
            comments.append((line_num, line))

            # Check if docstring ends on same line
            if stripped.count(docstring_delimiter) >= 2:
                in_docstring = False
        elif "#" in line:
            # Regular comment - extract everything after the first #
            comment_start = line.find("#")
            comment_text = line[comment_start:]
            comments.append((line_num, comment_text))

    return comments


def _has_allow_comment(file_path: Path, line_num: int) -> bool:
    """Check if line has ``# allow: timeless-comments`` comment."""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        if 0 < line_num <= len(lines):
            line_lower = lines[line_num - 1].lower()
            if "# allow:" in line_lower and "timeless-comments" in line_lower:
                return True
    except (UnicodeDecodeError, IndexError):
        pass
    return False


def check_timeless_comments(file_path: Path) -> list[tuple[int, str, str]]:
    """Check for temporal language in comments.

    Returns list of (line_number, comment_text, matched_keyword) tuples.
    """
    violations = []
    comments = extract_comments(file_path)

    for line_num, comment_text in comments:
        comment_lower = comment_text.lower()

        # Skip if line has allow comment
        if _has_allow_comment(file_path, line_num):
            continue

        # Skip if comment has exemption marker
        if "\u23f3" in comment_text or "temporal-ok" in comment_lower:
            continue

        # Skip lines with TODO or FIXME (they are inherently time-bound)
        if "todo" in comment_lower or "fixme" in comment_lower:
            continue

        # Check each temporal keyword
        for keyword in TEMPORAL_KEYWORDS:
            if re.search(keyword, comment_lower):
                violations.append((line_num, comment_text.strip(), keyword))
                break  # Only report one violation per line

    return violations


def main(filenames: list[str]) -> int:
    """Run timeless comment check on provided files."""
    exit_code = 0
    total_violations = 0

    for filename in filenames:
        file_path = Path(filename)

        # Only check Python files
        if file_path.suffix != ".py":
            continue

        # Skip if file doesn't exist (might be deleted)
        if not file_path.exists():
            continue

        violations = check_timeless_comments(file_path)

        if violations:
            exit_code = 1
            total_violations += len(violations)

            for line_num, _comment_text, keyword in violations:
                print(
                    f"{file_path}:{line_num}: Temporal keyword '{keyword}' "
                    f"in comment — rewrite to describe current behavior, "
                    f"not history (or mark with # temporal-ok)"
                )

    if exit_code:
        print(f"Found {total_violations} comment(s) to review; temporal words may be domain terms.")

    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advisory", action="store_true", help="Report findings without blocking")
    parser.add_argument("filenames", nargs="*")
    args = parser.parse_args()
    result = main(args.filenames)
    sys.exit(0 if args.advisory else result)
