#!/usr/bin/env python3
"""Prek hook to detect overly broad exception handling.

Philosophy: Do not use overly permissive exception handling that swallows
errors. Prefer to fail fast and fix the root cause rather than masking the
problem with a bare ``except:`` or a silent ``pass``.

Detects:
- Bare ``except:`` clauses
- ``except Exception:`` without re-raising
- Exception handlers whose body is only ``pass`` or ``continue``

Allowed:
- Lines annotated with ``# allow: exception-handling``

Exit codes:
  0 - All checks passed
  1 - Violations found
"""

import ast
import sys
from pathlib import Path


class ExceptionHandlerVisitor(ast.NodeVisitor):
    """AST visitor to find problematic exception handlers."""

    def __init__(self, filename: str, file_content: str) -> None:
        self.filename = filename
        self.file_content = file_content
        self.lines = file_content.splitlines()
        self.violations: list[tuple[int, str, str]] = []

    def _has_allow_comment(self, start_line: int, end_line: int | None = None) -> bool:
        """Check if any line in the range has ``# allow: exception-handling``."""
        if end_line is None:
            end_line = start_line
        for line_num in range(start_line, end_line + 1):
            if 0 < line_num <= len(self.lines):
                line_lower = self.lines[line_num - 1].lower()
                if "# allow:" in line_lower and "exception-handling" in line_lower:
                    return True
        return False

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Check each exception handler for violations."""
        line_num = node.lineno

        # Skip if line has allow comment
        if self._has_allow_comment(line_num):
            self.generic_visit(node)
            return

        # Check for bare except:
        if node.type is None:
            self.violations.append(
                (
                    line_num,
                    "bare_except",
                    (
                        "Bare 'except:' clause catches all exceptions including "
                        "KeyboardInterrupt — catch a specific exception type instead "
                        "(e.g., except ValueError as e:)"
                    ),
                )
            )
        # Check for except Exception:
        elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
            # Check if it re-raises
            has_raise = any(isinstance(stmt, ast.Raise) for stmt in node.body)

            # Check if it only has pass or continue
            is_only_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            is_only_continue = len(node.body) == 1 and isinstance(node.body[0], ast.Continue)

            if is_only_pass:
                self.violations.append(
                    (
                        line_num,
                        "exception_pass",
                        (
                            "Exception handler with only 'pass' swallows all errors "
                            "— log the error and re-raise, or catch a narrower type"
                        ),
                    )
                )
            elif is_only_continue:
                self.violations.append(
                    (
                        line_num,
                        "exception_continue",
                        (
                            "Exception handler with only 'continue' swallows all errors "
                            "— log the error and re-raise, or catch a narrower type"
                        ),
                    )
                )
            elif not has_raise:
                # Only flag if there's no logging or other meaningful action
                has_logging = any(
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr
                    in ("error", "warning", "exception", "critical", "debug", "info")
                    for stmt in node.body
                )

                if not has_logging:
                    self.violations.append(
                        (
                            line_num,
                            "broad_exception",
                            (
                                "Broad 'except Exception' without logging or re-raising "
                                "— catch a specific exception or add logger.exception()"
                            ),
                        )
                    )

        self.generic_visit(node)


def check_exception_handling(file_path: Path) -> list[tuple[int, str, str]]:
    """Check for problematic exception handling in a Python file.

    Returns list of (line_number, violation_type, message) tuples.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))

        visitor = ExceptionHandlerVisitor(str(file_path), content)
        visitor.visit(tree)

    except SyntaxError:
        # Skip files with syntax errors (will be caught by other tools)
        return []
    except UnicodeDecodeError:
        # Skip binary files
        return []
    else:
        return visitor.violations


def main(filenames: list[str]) -> int:
    """Run exception handling check on provided files."""
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

        violations = check_exception_handling(file_path)

        if violations:
            exit_code = 1
            total_violations += len(violations)

            for line_num, _violation_type, message in violations:
                print(f"{file_path}:{line_num}: {message}")

    if exit_code != 0:
        print("\n" + "=" * 70)
        print(f"Found {total_violations} exception handling violation(s).")
        print()
        print("  FIX the code (preferred):")
        print("     BAD:  except Exception: pass")
        print("     GOOD: except (ValueError, KeyError) as e:")
        print("               logger.error('Failed to process: %s', e)")
        print()
        print("  EXEMPT with '# allow: exception-handling' ONLY when:")
        print("     - The exception IS handled (collected, user notification)")
        print("     - Fallback for unsupported operation (e.g., clipboard)")
        print("     - Errors are appended to a list for batch reporting")
        print("=" * 70)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
