"""Audit semantic action markers without collecting the entire host test suite."""

from __future__ import annotations

import argparse
import ast
import os
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


@dataclass(frozen=True)
class _PytestBindings:
    module_aliases: frozenset[str]
    mark_aliases: frozenset[str]


def _attribute_path(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _pytest_bindings(tree: ast.AST) -> _PytestBindings:
    module_aliases = {"pytest"}
    mark_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or "pytest" for alias in node.names if alias.name == "pytest"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            mark_aliases.update(
                alias.asname or "mark" for alias in node.names if alias.name == "mark"
            )
    return _PytestBindings(frozenset(module_aliases), frozenset(mark_aliases))


def _is_pytest_call(call: ast.Call, bindings: _PytestBindings, *attributes: str) -> bool:
    path = _attribute_path(call.func)
    return (
        path is not None
        and len(path) == len(attributes) + 1
        and (path[0] in bindings.module_aliases and path[1:] == attributes)
    )


def _is_action_reference(node: ast.expr, bindings: _PytestBindings) -> bool:
    path = _attribute_path(node)
    if path is None:
        return False
    return (
        len(path) == 3 and path[0] in bindings.module_aliases and path[1:] == ("mark", "action")
    ) or (len(path) == 2 and path[0] in bindings.mark_aliases and path[1] == "action")


def _is_action_call(call: ast.Call, bindings: _PytestBindings) -> bool:
    return _is_action_reference(call.func, bindings)


def _action_calls_in_marks(node: ast.expr, bindings: _PytestBindings) -> tuple[ast.Call, ...]:
    if isinstance(node, ast.Call) and _is_action_call(node, bindings):
        return (node,)
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(
            action_call
            for element in node.elts
            for action_call in _action_calls_in_marks(element, bindings)
        )
    return ()


def _attached_action_calls(tree: ast.AST, bindings: _PytestBindings) -> set[int]:
    attached: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            attached.update(
                id(decorator)
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call) and _is_action_call(decorator, bindings)
            )
        if not isinstance(node, ast.Call) or not _is_pytest_call(node, bindings, "param"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "marks":
                attached.update(
                    id(action_call)
                    for action_call in _action_calls_in_marks(keyword.value, bindings)
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

    bindings = _pytest_bindings(tree)
    action_factory_aliases = sorted(
        (
            node.value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and node.value is not None
            and _is_action_reference(node.value, bindings)
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    if action_factory_aliases:
        alias_errors = [
            f"{_location(path, value)}: action marker factory aliases are not supported; "
            "call pytest.mark.action directly"
            for value in action_factory_aliases
        ]
        raise ActionCoverageAuditError("\n".join(alias_errors))

    attached = _attached_action_calls(tree, bindings)
    action_calls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_action_call(node, bindings)
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
    environment = os.environ.copy()
    environment["PYTEST_ADDOPTS"] = ""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module and flags.
        pytest_collection_command(marker_files),
        check=False,
        env=environment,
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
