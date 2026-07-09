#!/usr/bin/env python3
"""Pre-commit hook to forbid tests from importing private first-party symbols.

Philosophy: tests should verify *public behaviour*, not private implementation
shape. Importing a leading-underscore name from a first-party package into a
test couples that test to internal structure: it breaks on harmless refactors
and, worse, keeps dead private code alive past the point the public surface
stopped needing it. Drive the public entry point instead and assert on its
observable result.

Detects:
- ``from <first_party>... import _private`` in test files, including the
  per-name form inside a parenthesised multi-line import

First-party packages are auto-detected from the repository layout (top-level
or ``src/`` directories containing ``__init__.py``). Pass ``--package NAME``
(repeatable) to override detection.

Allowed:
- Public names (no leading underscore) and dunders (``__version__``)
- Private imports from non-first-party modules (test-support helpers, stdlib,
  third-party packages)
- A name annotated with ``# allow: private-test-imports`` (narrow carve-out for
  a private whose only effect is an external-process side channel with no
  public observable)

Exit codes:
  0 - All checks passed
  1 - Violations found
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

_ALLOW_MARKER = "private-test-imports"
_SKIP_DIRS = frozenset(
    {
        "tests",
        "test",
        "scripts",
        "docs",
        "doc",
        "examples",
        "build",
        "dist",
        "node_modules",
        "__pycache__",
    }
)


def detect_first_party_packages(root: Path) -> set[str]:
    """Return the set of importable top-level first-party package names.

    A first-party package is a directory containing ``__init__.py`` at the repo
    root or under ``src/``. Tooling and test directories are excluded. When the
    repo ships no importable package (e.g. a docs-only repo) the result is empty
    and the hook flags nothing.
    """
    packages: set[str] = set()
    search_roots = [root, root / "src"]
    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        for child in search_root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in _SKIP_DIRS:
                continue
            if (child / "__init__.py").is_file():
                packages.add(child.name)
    return packages


class PrivateImportVisitor(ast.NodeVisitor):
    """AST visitor flagging imports of private first-party symbols."""

    def __init__(self, file_content: str, first_party: set[str]) -> None:
        self.lines = file_content.splitlines()
        self.first_party = first_party
        self.violations: list[tuple[int, str, str]] = []

    def _is_first_party(self, module: str) -> bool:
        """Return True when *module*'s top component is a first-party package."""
        top = module.split(".", 1)[0]
        return top in self.first_party

    def _is_private(self, name: str) -> bool:
        """Return True when imported *name* is a private symbol.

        A leading underscore marks private. A ``__dunder__`` (leading AND
        trailing double underscore) is public API and is not flagged; a
        name-mangled ``__thing`` (no trailing dunder) stays private.
        """
        return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))

    def _has_allow_comment(self, line_num: int) -> bool:
        if 0 < line_num <= len(self.lines):
            line_lower = self.lines[line_num - 1].lower()
            return "# allow:" in line_lower and _ALLOW_MARKER in line_lower
        return False

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if self._is_first_party(module):
            for alias in node.names:
                if self._is_private(alias.name) and not self._has_allow_comment(alias.lineno):
                    self.violations.append((alias.lineno, alias.name, module))
        self.generic_visit(node)


def find_private_imports(
    content: str,
    first_party: set[str],
    filename: str = "<unknown>",
) -> list[tuple[int, str, str]]:
    """Return ``(lineno, name, module)`` for each private first-party import."""
    try:
        tree = ast.parse(content, filename=filename)
    except (SyntaxError, ValueError):
        return []
    visitor = PrivateImportVisitor(content, first_party)
    visitor.visit(tree)
    return visitor.violations


def is_test_file(file_path: Path) -> bool:
    """Only test files are subject to the convention."""
    return "tests/" in str(file_path) or file_path.name.startswith("test_")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Forbid private first-party imports in tests")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="First-party package name to guard (repeatable; overrides auto-detection)",
    )
    parser.add_argument("filenames", nargs="*")
    args = parser.parse_args(argv)

    first_party = set(args.package) or detect_first_party_packages(Path.cwd())
    if not first_party:
        return 0

    exit_code = 0
    total = 0
    for filename in args.filenames:
        file_path = Path(filename)
        if file_path.suffix != ".py" or not is_test_file(file_path) or not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8")
        for line_num, name, module in find_private_imports(content, first_party, str(file_path)):
            exit_code = 1
            total += 1
            print(
                f"{file_path}:{line_num}: test imports private '{name}' from first-party "
                f"'{module}' — drive the public entry point that exercises it and assert on "
                f"observable output; if nothing public reaches it, the private is dead code"
            )

    if exit_code != 0:
        print("\n" + "=" * 70)
        print(f"Found {total} private-symbol import(s) in tests.")
        print()
        print("  Tests must verify PUBLIC behaviour, not private implementation shape.")
        print()
        print("  FIX: drive the public entry point that exercises this private and")
        print("       assert on its observable result. If nothing public reaches the")
        print("       private, it may be dead code — delete it (a 100% coverage gate")
        print("       is the arbiter).")
        print()
        print("  CARVE-OUT: a private whose only effect is an external-process side")
        print("       channel with no public observable may keep a focused test —")
        print(f"       annotate its import with '# allow: {_ALLOW_MARKER}'.")
        print("=" * 70)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
