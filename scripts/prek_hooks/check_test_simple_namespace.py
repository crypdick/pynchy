"""Reject ``types.SimpleNamespace`` in test code.

Tests need declared data shapes: a dataclass, a Pydantic model, or a focused
fake for behavior.  ``SimpleNamespace`` hides the contract they are meant to
exercise, so a changed boundary can leave a test silently stale.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def find_simple_namespace_uses(source: str) -> list[int]:
    """Return source lines that import or access ``types.SimpleNamespace``."""
    tree = ast.parse(source)
    types_aliases: set[str] = set()
    simple_namespace_aliases: set[str] = set()
    violations: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "types":
                    types_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "types":
            for alias in node.names:
                if alias.name == "SimpleNamespace":
                    simple_namespace_aliases.add(alias.asname or alias.name)
                    violations.add(node.lineno)

    for node in ast.walk(tree):
        is_direct_alias = isinstance(node, ast.Name) and node.id in simple_namespace_aliases
        is_qualified_access = (
            isinstance(node, ast.Attribute)
            and node.attr == "SimpleNamespace"
            and isinstance(node.value, ast.Name)
            and node.value.id in types_aliases
        )
        if is_direct_alias or is_qualified_access:
            violations.add(node.lineno)

    return sorted(violations)


def main(filenames: list[str] | None = None) -> int:
    """Print policy violations for the test files passed by prek."""
    paths = [Path(name) for name in (filenames if filenames is not None else sys.argv[1:])]
    violations = 0
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            lines = find_simple_namespace_uses(source)
        except (OSError, SyntaxError):
            continue
        for line in lines:
            violations += 1
            print(
                f"{path}:{line}: tests must not use types.SimpleNamespace — "
                "use a typed dataclass, Pydantic model, or focused fake instead"
            )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
