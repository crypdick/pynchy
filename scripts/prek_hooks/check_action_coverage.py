"""Audit semantic action markers without collecting the entire host test suite."""

from __future__ import annotations

import argparse
import ast
import subprocess  # noqa: S404 - invokes the current Python interpreter with fixed pytest flags.
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pynchy.actions import ACTION_SPECS, ActionSpec, assess_hermetic_coverage

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class ActionCoverageAuditError(ValueError):
    """An invalid marker declaration or incomplete action contract."""


@dataclass(frozen=True)
class ActionMarkerAudit:
    """Literal action IDs and files containing their attached markers."""

    action_ids: tuple[str, ...]
    marker_files: tuple[Path, ...]


def _attribute_path(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _pytest_aliases(tree: ast.AST) -> frozenset[str]:
    aliases = {"pytest"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        aliases.update(alias.asname or "pytest" for alias in node.names if alias.name == "pytest")
    return frozenset(aliases)


def _is_pytest_call(call: ast.Call, aliases: frozenset[str], *attributes: str) -> bool:
    path = _attribute_path(call.func)
    return (
        path is not None
        and len(path) == len(attributes) + 1
        and (path[0] in aliases and path[1:] == attributes)
    )


def _action_calls_in_marks(node: ast.expr, aliases: frozenset[str]) -> tuple[ast.Call, ...]:
    if isinstance(node, ast.Call) and _is_pytest_call(node, aliases, "mark", "action"):
        return (node,)
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(
            action_call
            for element in node.elts
            for action_call in _action_calls_in_marks(element, aliases)
        )
    return ()


def _attached_action_calls(tree: ast.AST, aliases: frozenset[str]) -> set[int]:
    attached: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            attached.update(
                id(decorator)
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and _is_pytest_call(decorator, aliases, "mark", "action")
            )
        if not isinstance(node, ast.Call) or not _is_pytest_call(node, aliases, "param"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "marks":
                attached.update(
                    id(action_call)
                    for action_call in _action_calls_in_marks(keyword.value, aliases)
                )
    return attached


def _location(path: Path, node: ast.AST) -> str:
    return f"{path}:{getattr(node, 'lineno', 1)}:{getattr(node, 'col_offset', 0) + 1}"


def extract_action_markers(source: str, *, path: Path) -> tuple[str, ...]:
    """Extract attached literal action IDs or reject an unauditable declaration."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        line = error.lineno or 1
        column = error.offset or 1
        raise ActionCoverageAuditError(
            f"{path}:{line}:{column}: invalid Python syntax: {error.msg}"
        ) from error

    aliases = _pytest_aliases(tree)
    attached = _attached_action_calls(tree, aliases)
    action_calls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_pytest_call(node, aliases, "mark", "action")
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    action_ids: list[str] = []
    errors: list[str] = []
    for call in action_calls:
        location = _location(path, call)
        if id(call) not in attached:
            errors.append(
                f"{location}: action marker must decorate a function/class or appear "
                "in pytest.param(..., marks=...)"
            )
            continue
        if call.keywords:
            errors.append(f"{location}: action marker keyword arguments are not supported")
        if not call.args:
            errors.append(f"{location}: action marker requires at least one string literal ID")
        for argument in call.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                action_ids.append(argument.value)
            else:
                errors.append(
                    f"{_location(path, argument)}: action marker IDs must be string literals"
                )
    if errors:
        raise ActionCoverageAuditError("\n".join(errors))
    return tuple(action_ids)


def _python_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(candidate for candidate in path.rglob("*.py") if candidate.is_file())
        elif path.is_file() and path.suffix == ".py":
            files.add(path)
        else:
            raise ActionCoverageAuditError(f"{path}: expected a Python file or directory")
    if not files:
        raise ActionCoverageAuditError("no Python test files found")
    return tuple(sorted(files))


def audit_action_coverage(
    paths: Iterable[Path], specs: Iterable[ActionSpec] = ACTION_SPECS
) -> ActionMarkerAudit:
    """Validate static marker syntax and compare literal IDs with the action catalog."""
    action_ids: list[str] = []
    marker_files: list[Path] = []
    errors: list[str] = []
    for path in _python_files(paths):
        try:
            file_action_ids = extract_action_markers(
                path.read_text(encoding="utf-8"),
                path=path,
            )
        except (ActionCoverageAuditError, OSError) as error:
            errors.append(str(error))
            continue
        if file_action_ids:
            marker_files.append(path)
            action_ids.extend(file_action_ids)
    if errors:
        raise ActionCoverageAuditError("\n".join(errors))

    report = assess_hermetic_coverage(specs, action_ids)
    if not report.is_complete:
        raise ActionCoverageAuditError(f"Action coverage incomplete: {report.describe()}")
    return ActionMarkerAudit(tuple(action_ids), tuple(marker_files))


def pytest_collection_command(marker_files: Sequence[Path]) -> tuple[str, ...]:
    """Build the authoritative, marker-file-scoped pytest collection command."""
    if not marker_files:
        raise ActionCoverageAuditError(
            "no attached action marker files found; refusing to collect the entire test suite"
        )
    return (
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-n",
        "0",
        "--action-coverage",
        "-qq",
        *(str(path) for path in marker_files),
    )


def collect_marked_tests(marker_files: Sequence[Path]) -> int:
    """Ask pytest to prove the statically valid markers attach to collected tests."""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module and flags.
        pytest_collection_command(marker_files),
        check=False,
    )
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    """Run the static audit and scoped authoritative pytest collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("tests")])
    args = parser.parse_args(argv)
    try:
        audit = audit_action_coverage(args.paths)
    except ActionCoverageAuditError as error:
        print(f"Action coverage audit failed:\n{error}", file=sys.stderr)
        return 1
    return collect_marked_tests(audit.marker_files)


if __name__ == "__main__":
    raise SystemExit(main())
