"""Reject hard-coded prompt-content assertions in tests."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_prompt_value(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "prompt" or node.id.endswith("_prompt")
    if isinstance(node, ast.Attribute):
        return node.attr == "prompt" or node.attr.endswith("_prompt")
    return False


def find_prompt_assertions(source: str) -> list[int]:
    """Return lines with literal string membership assertions against prompts."""
    tree = ast.parse(source)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
        and isinstance(node.test, ast.Compare)
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.In)
        and isinstance(node.test.left, ast.Constant)
        and isinstance(node.test.left.value, str)
        and len(node.test.comparators) == 1
        and _is_prompt_value(node.test.comparators[0])
    )


def main(filenames: list[str] | None = None) -> int:
    """Print hard-coded prompt assertions in test files."""
    violations = 0
    for filename in filenames if filenames is not None else sys.argv[1:]:
        path = Path(filename)
        if path.suffix != ".py" or not path.exists():
            continue
        try:
            lines = find_prompt_assertions(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for line in lines:
            violations += 1
            print(
                f"{path}:{line}: do not assert hard-coded prompt text; "
                "assert prompt structure, IDs, or other observable behavior"
            )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
