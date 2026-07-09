#!/usr/bin/env python3
"""Pre-commit hook to detect print() calls and unstructured logging in production code.

Philosophy: Production code should use structured logging (the ``logging``
module) instead of ``print()``.  When using a logger, prefer parameterised
messages with ``extra={}`` over string formatting so that log aggregation
tools can index fields.

Detects:
- ``print()`` calls in production code
- String concatenation inside logger calls (``logger.info("x: " + val)``)
- f-string formatting inside logger calls (``logger.info(f"x: {val}")``)

Allowed locations (print only):
- Test files (``tests/``, ``test_*.py``)
- Script/tool directories (``scripts/``, ``tools/``, ``_tools/``)
- CLI entry points (``__main__.py``, ``cli.py``, ``main.py``)
- Lines with ``# allow: print-statements``

Exit codes:
  0 - All checks passed
  1 - Violations found
"""

import ast
import sys
from pathlib import Path

# Logger method names we care about
_LOGGER_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
        "exception",
    }
)


class PrintStatementVisitor(ast.NodeVisitor):
    """AST visitor to find ``print()`` calls."""

    def __init__(self, filename: str, file_content: str) -> None:
        self.filename = filename
        self.file_content = file_content
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Check if this is a ``print()`` call."""
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            line_num = node.lineno
            end_line = getattr(node, "end_lineno", line_num) or line_num
            if not self._has_allow_comment(line_num, end_line):
                self.violations.append(
                    (
                        line_num,
                        (
                            "print() call in production code — use logger.info() "
                            "or logger.debug() instead"
                        ),
                    )
                )

        self.generic_visit(node)

    def _has_allow_comment(self, start_line: int, end_line: int | None = None) -> bool:
        """Check if any line in the range has ``# allow: print-statements``."""
        if end_line is None:
            end_line = start_line
        lines = self.file_content.split("\n")
        for line_num in range(start_line, end_line + 1):
            if 0 < line_num <= len(lines):
                line_lower = lines[line_num - 1].lower()
                if "# allow:" in line_lower and "print-statements" in line_lower:
                    return True
        return False


class UnstructuredLoggingVisitor(ast.NodeVisitor):
    """AST visitor to find string formatting inside logger calls.

    Structured logging passes data via ``extra={}`` so that log aggregation
    systems can index individual fields.  String interpolation bakes the data
    into the message string, defeating this.

    Flags:
    - ``logger.info("user: " + user_id)``   (string concatenation)
    - ``logger.info(f"user: {user_id}")``    (f-string formatting)
    """

    def __init__(self, filename: str, file_content: str) -> None:
        self.filename = filename
        self.file_content = file_content
        self.lines = file_content.splitlines()
        self.violations: list[tuple[int, str]] = []

    def _has_allow_comment(self, line_num: int) -> bool:
        if 0 < line_num <= len(self.lines):
            line_lower = self.lines[line_num - 1].lower()
            if "# allow:" in line_lower and "unstructured-logging" in line_lower:
                return True
        return False

    def visit_Call(self, node: ast.Call) -> None:
        """Detect string formatting in logger method calls."""
        if isinstance(node.func, ast.Attribute) and node.func.attr in _LOGGER_METHODS and node.args:
            first_arg = node.args[0]
            line_num = node.lineno

            if self._has_allow_comment(line_num):
                self.generic_visit(node)
                return

            # Detect string concatenation: logger.info("msg: " + val)
            if isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, ast.Add):
                self.violations.append(
                    (
                        line_num,
                        (
                            "String concatenation in logger call "
                            "— use structured logging instead: "
                            "logger.info('event description', extra={'key': value})"
                        ),
                    )
                )

            # Detect f-string: logger.info(f"msg: {val}")
            elif isinstance(first_arg, ast.JoinedStr):
                self.violations.append(
                    (
                        line_num,
                        (
                            "f-string formatting in logger call "
                            "— use structured logging instead: "
                            "logger.info('event description', extra={'key': value})"
                        ),
                    )
                )

        self.generic_visit(node)


def check_print_statements(file_path: Path) -> list[tuple[int, str]]:
    """Check for ``print()`` calls in a Python file.

    Returns list of (line_number, message) tuples.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))

        visitor = PrintStatementVisitor(str(file_path), content)
        visitor.visit(tree)

    except SyntaxError:
        return []
    except UnicodeDecodeError:
        return []
    else:
        return visitor.violations


def check_unstructured_logging(file_path: Path) -> list[tuple[int, str]]:
    """Check for unstructured logging patterns in a Python file.

    Returns list of (line_number, message) tuples.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))

        visitor = UnstructuredLoggingVisitor(str(file_path), content)
        visitor.visit(tree)

    except SyntaxError:
        return []
    except UnicodeDecodeError:
        return []
    else:
        return visitor.violations


def is_allowed_location(file_path: Path) -> bool:
    """Check if ``print()`` is allowed in this file location."""
    path_str = str(file_path)

    # Allow in test files
    if "tests/" in path_str or file_path.name.startswith("test_"):
        return True

    # Allow in scripts and tools
    if any(marker in path_str for marker in ["scripts/", "/tools/", "/_tools/"]):
        return True

    # Allow in __main__.py and CLI entry points
    return file_path.name in ("__main__.py", "cli.py", "main.py")


def main(filenames: list[str]) -> int:
    """Run print statement and unstructured logging checks on provided files."""
    exit_code = 0
    total_violations = 0

    for filename in filenames:
        file_path = Path(filename)

        # Only check Python files
        if file_path.suffix != ".py":
            continue

        # Skip if file doesn't exist
        if not file_path.exists():
            continue

        # --- print() checks (skip in allowed locations) ---
        if not is_allowed_location(file_path):
            print_violations = check_print_statements(file_path)
            if print_violations:
                exit_code = 1
                total_violations += len(print_violations)
                for line_num, message in print_violations:
                    print(f"{file_path}:{line_num}: {message}")

        # --- unstructured-logging checks (always active) ---
        log_violations = check_unstructured_logging(file_path)
        if log_violations:
            exit_code = 1
            total_violations += len(log_violations)
            for line_num, message in log_violations:
                print(f"{file_path}:{line_num}: {message}")

    if exit_code != 0:
        print("\n" + "=" * 70)
        print(f"Found {total_violations} violation(s).")
        print()
        print("  FIX print() calls (preferred):")
        print("     BAD:  print('Processing:', data)")
        print("     GOOD: logger.info('Processing item', extra={'data': data})")
        print()
        print("  FIX unstructured logging:")
        print("     BAD:  logger.info(f'user: {user_id}')")
        print("     BAD:  logger.info('user: ' + user_id)")
        print("     GOOD: logger.info('user logged in', extra={'user_id': user_id})")
        print()
        print("  EXEMPT with '# allow: print-statements' or")
        print("  '# allow: unstructured-logging' ONLY when justified.")
        print("=" * 70)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
