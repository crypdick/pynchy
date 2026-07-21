#!/usr/bin/env python3
"""Forbid tests from coupling to private first-party implementation details.

Tests should verify public behaviour, not private implementation shape.  A
leading underscore is the codebase's declaration that a symbol, module, or
state field may change without preserving compatibility.  Tests that reach
across that boundary make harmless refactors expensive and can keep dead code
alive after its public behaviour has disappeared.

The check covers private first-party:

* imported symbols and module paths;
* attributes reached through an imported module/class or a directly-created
  first-party instance, including typed fixtures and typed local factories;
* literal dynamic imports of private first-party modules.

Dotted strings passed to patching helpers are deliberately out of scope. They
substitute a collaborator while a test drives a public operation; a string
alone does not show that the test asserts a private object's shape. Enforcing
those strings would reject legitimate public-behaviour tests and force tests
to promote implementation helpers merely to satisfy the checker.

``--baseline-ref`` provides a temporary ratchet for a large existing suite.
Violations already present in that Git ref are tolerated by exact AST shape and
occurrence count; new or materially changed violations fail.  Removing a
violation therefore shrinks the debt automatically, without a hand-maintained
suppression file.
"""

from __future__ import annotations

import argparse
import ast
import subprocess  # noqa: S404 - invokes fixed Git argv for local baseline comparison.
import sys
from collections import Counter
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from tokenize import COMMENT, TokenError, generate_tokens

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


@dataclass(frozen=True)
class PrivateTestViolation:
    """A private first-party boundary crossed by a test."""

    line: int
    kind: str
    subject: str
    shape: str

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        """Return a location-independent identity for baseline comparison."""
        return (self.kind, self.subject, self.shape)


def detect_first_party_packages(root: Path) -> set[str]:
    """Return the importable top-level first-party package names at *root*."""
    packages: set[str] = set()
    for search_root in (root, root / "src"):
        if not search_root.is_dir():
            continue
        for child in search_root.iterdir():
            if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if (child / "__init__.py").is_file():
                packages.add(child.name)
    return packages


def _is_private(name: str) -> bool:
    """Return whether *name* is private while allowing dunder public API."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _first_name(node: ast.expr) -> str | None:
    """Return the left-most name in an attribute expression."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _annotation_names(annotation: ast.expr | None) -> set[str]:
    """Return names appearing in an annotation, including nested generic types."""
    names: set[str] = set()
    if annotation is None:
        return names
    if isinstance(annotation, ast.Name):
        names.add(annotation.id)
    elif isinstance(annotation, ast.Attribute):
        first_name = _first_name(annotation)
        if first_name is not None:
            names.add(first_name)
    elif isinstance(annotation, ast.Subscript):
        names.update(_annotation_names(annotation.value))
        names.update(_annotation_names(annotation.slice))
    elif isinstance(annotation, ast.Tuple):
        for element in annotation.elts:
            names.update(_annotation_names(element))
    elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        names.update(_annotation_names(annotation.left))
        names.update(_annotation_names(annotation.right))
    elif isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            forward_reference = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            names.add(annotation.value.split(".", maxsplit=1)[0])
        else:
            names.update(_annotation_names(forward_reference))
    return names


def _annotation_mentions_first_party(annotation: ast.expr | None, known: set[str]) -> bool:
    """Return whether an annotation names a statically known first-party object."""
    return bool(_annotation_names(annotation) & known)


def _self_or_cls_attribute(node: ast.expr) -> tuple[str, str] | None:
    """Return a direct ``self``/``cls`` attribute reference, when present."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"self", "cls"}
    ):
        return (node.value.id, node.attr)
    return None


def _has_private_component(path: str, first_party: set[str]) -> bool:
    """Return whether dotted *path* is first-party and crosses a private part."""
    components = path.split(".")
    has_private_part = any(map(_is_private, components[1:]))
    return bool(components and components[0] in first_party and has_private_part)


def _allow_reason(line: str) -> str | None:
    """Return an explicit external-process carve-out reason, if present."""
    marker = line.lower().find(_ALLOW_MARKER)
    if "# allow:" not in line.lower() or marker < 0:
        return None
    reason = line[marker + len(_ALLOW_MARKER) :].strip(" -:\t")
    return reason if reason.lower().startswith("external-process:") else None


def _allow_reasons(source: str) -> dict[int, str | None]:
    """Return explicit allow-marker comments keyed by their source line."""
    reasons: dict[int, str | None] = {}
    try:
        tokens = tuple(generate_tokens(StringIO(source).readline))
    except TokenError:
        return reasons
    for token in tokens:
        if token.type != COMMENT or _ALLOW_MARKER not in token.string.lower():
            continue
        if "# allow:" not in token.string.lower():
            continue
        reasons[token.start[0]] = _allow_reason(token.string)
    return reasons


class _FirstPartyBindingCollector(ast.NodeVisitor):
    """Find names that unambiguously refer to a first-party module or object."""

    def __init__(self, first_party: set[str]) -> None:
        self.first_party = first_party
        self.bound_names: set[str] = set()
        self.factory_method_names: set[str] = set()
        self.import_module_names: set[str] = set()
        self.importlib_module_names: set[str] = set()
        self.assignments: list[ast.Assign | ast.AnnAssign] = []
        self._class_depth = 0

    def _is_first_party_module(self, module: str) -> bool:
        return module.split(".", 1)[0] in self.first_party

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self._is_first_party_module(alias.name):
                self.bound_names.add(alias.asname or alias.name.split(".", 1)[0])
            if alias.name == "importlib":
                self.importlib_module_names.add(alias.asname or "importlib")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if self._is_first_party_module(module):
            self.bound_names.update(alias.asname or alias.name for alias in node.names)
        if module == "importlib":
            self.import_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assignments.append(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.assignments.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._collect_factory(node)
        self._collect_typed_arguments(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._collect_factory(node)
        self._collect_typed_arguments(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Track when a function is a test-class factory method."""
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def _collect_factory(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Track local factories that construct a known first-party object."""
        is_typed_factory = _annotation_mentions_first_party(node.returns, self.bound_names)
        directly_constructs_first_party = any(
            isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Call)
            and _first_name(statement.value.func) in self.bound_names
            for statement in ast.walk(node)
        )
        if not is_typed_factory and not directly_constructs_first_party:
            return
        self.bound_names.add(node.name)
        if self._class_depth:
            self.factory_method_names.add(node.name)

    def _collect_typed_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
        if arguments.vararg is not None:
            all_arguments = (*all_arguments, arguments.vararg)
        if arguments.kwarg is not None:
            all_arguments = (*all_arguments, arguments.kwarg)
        for argument in all_arguments:
            if _annotation_mentions_first_party(argument.annotation, self.bound_names):
                self.bound_names.add(argument.arg)

    def resolve_instances(self) -> tuple[set[str], set[tuple[str, str]]]:
        """Infer simple ``thing = FirstPartyClass(...)`` bindings.

        This is intentionally shallow.  The checker must not try to become a
        second type checker; module/class aliases and direct construction cover
        the unambiguous cases while avoiding test-double false positives.
        """
        known = set(self.bound_names)
        known_attributes: set[tuple[str, str]] = set()
        for assignment in sorted(self.assignments, key=lambda item: item.lineno):
            value = assignment.value
            if value is None:
                continue
            if not self._is_known_instance_value(value, known, known_attributes):
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    known.add(target.id)
                elif attribute := _self_or_cls_attribute(target):
                    known_attributes.add(attribute)
        return known, known_attributes

    def _is_known_instance_value(
        self,
        value: ast.expr,
        known: set[str],
        known_attributes: set[tuple[str, str]],
    ) -> bool:
        """Return whether an expression statically aliases a first-party object."""
        if isinstance(value, ast.Call):
            return self._is_known_factory_call(value.func, known) or self._is_typed_cast(
                value, known
            )
        if isinstance(value, ast.Name):
            return value.id in known
        return _self_or_cls_attribute(value) in known_attributes

    def _is_typed_cast(self, value: ast.Call, known: set[str]) -> bool:
        """Return whether ``typing.cast`` explicitly asserts a first-party type."""
        is_cast = (isinstance(value.func, ast.Name) and value.func.id == "cast") or (
            isinstance(value.func, ast.Attribute) and value.func.attr == "cast"
        )
        return (
            is_cast and bool(value.args) and _annotation_mentions_first_party(value.args[0], known)
        )

    def _is_known_factory_call(self, func: ast.expr, known: set[str]) -> bool:
        """Return whether a call has a statically known first-party result."""
        if _first_name(func) in known:
            return True
        return (
            isinstance(func, ast.Attribute)
            and func.attr in self.factory_method_names
            and _first_name(func.value) in {"self", "cls"}
        )


class PrivateTestBoundaryVisitor(ast.NodeVisitor):
    """Report private first-party imports and direct attribute access."""

    def __init__(
        self,
        source: str,
        first_party: set[str],
        bound_names: set[str],
        bound_attributes: set[tuple[str, str]],
        import_module_names: set[str],
        importlib_module_names: set[str],
        allow_reasons: dict[int, str | None],
    ) -> None:
        self.lines = source.splitlines()
        self.first_party = first_party
        self.bound_names = bound_names
        self.bound_attributes = bound_attributes
        self.import_module_names = import_module_names
        self.importlib_module_names = importlib_module_names
        self.allow_reasons = allow_reasons
        self.violations: list[PrivateTestViolation] = []

    def _add(self, node: ast.AST, kind: str, subject: str) -> None:
        line = getattr(node, "lineno", 0)
        if 0 < line <= len(self.lines) and self.allow_reasons.get(line):
            return
        self.violations.append(
            PrivateTestViolation(
                line=line,
                kind=kind,
                subject=subject,
                shape=ast.dump(node, include_attributes=False),
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _has_private_component(alias.name, self.first_party):
                self._add(alias, "private-module-import", alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not _has_private_component(module, self.first_party) and (
            module.split(".", 1)[0] not in self.first_party
        ):
            return
        if _has_private_component(module, self.first_party):
            self._add(node, "private-module-import", module)
        if module.split(".", 1)[0] in self.first_party:
            for alias in node.names:
                if _is_private(alias.name):
                    self._add(alias, "private-symbol-import", f"{module}.{alias.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Reject literal calls that bypass normal attribute/import syntax checks."""
        if self._is_dynamic_import(node) and node.args:
            module_name = node.args[0]
            if (
                isinstance(module_name, ast.Constant)
                and isinstance(module_name.value, str)
                and _has_private_component(module_name.value, self.first_party)
            ):
                self._add(node, "private-dynamic-module-import", module_name.value)
        self._check_dynamic_private_attribute(node)
        self.generic_visit(node)

    def _check_dynamic_private_attribute(self, node: ast.Call) -> None:
        """Reject ``getattr``-style literal reads of a known private attribute."""
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in {"getattr", "hasattr", "setattr", "delattr"}
            or len(node.args) < 2
        ):
            return
        target, attribute = node.args[:2]
        if not (
            isinstance(attribute, ast.Constant)
            and isinstance(attribute.value, str)
            and _is_private(attribute.value)
            and self._is_known_first_party_target(target)
        ):
            return
        self._add(
            node,
            "private-dynamic-attribute",
            f"{ast.unparse(target)}.{attribute.value}",
        )

    def _is_known_first_party_target(self, node: ast.expr) -> bool:
        """Return whether a dynamic-attribute target is statically first party."""
        return (
            _first_name(node) in self.bound_names
            or _self_or_cls_attribute(node) in self.bound_attributes
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in self.bound_names
            )
        )

    def _is_dynamic_import(self, node: ast.Call) -> bool:
        """Return whether *node* invokes an imported Python module loader."""
        if isinstance(node.func, ast.Name):
            return node.func.id in {"__import__", *self.import_module_names}
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and _first_name(node.func.value) in self.importlib_module_names
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_private(node.attr) and self._is_known_first_party_target(node.value):
            self._add(node, "private-attribute", ast.unparse(node))
        self.generic_visit(node)


def find_private_test_violations(
    content: str,
    first_party: set[str],
    filename: str = "<unknown>",
) -> list[PrivateTestViolation]:
    """Return every private first-party boundary violation in test *content*."""
    try:
        tree = ast.parse(content, filename=filename)
    except (SyntaxError, ValueError):
        return []
    bindings = _FirstPartyBindingCollector(first_party)
    bindings.visit(tree)
    allow_reasons = _allow_reasons(content)
    bound_names, bound_attributes = bindings.resolve_instances()
    visitor = PrivateTestBoundaryVisitor(
        content,
        first_party,
        bound_names,
        bound_attributes,
        bindings.import_module_names,
        bindings.importlib_module_names,
        allow_reasons,
    )
    visitor.visit(tree)
    for line_num, reason in allow_reasons.items():
        if reason is None:
            line = content.splitlines()[line_num - 1]
            visitor.violations.append(
                PrivateTestViolation(
                    line=line_num,
                    kind="unjustified-allow",
                    subject=_ALLOW_MARKER,
                    shape=line.strip(),
                )
            )
    return sorted(visitor.violations, key=lambda item: (item.line, item.kind, item.subject))


def find_private_imports(
    content: str,
    first_party: set[str],
    filename: str = "<unknown>",
) -> list[tuple[int, str, str]]:
    """Return private symbol imports in the legacy hook result shape."""
    return [
        (violation.line, violation.subject.rsplit(".", 1)[-1], violation.subject.rsplit(".", 1)[0])
        for violation in find_private_test_violations(content, first_party, filename)
        if violation.kind == "private-symbol-import"
    ]


def _read_ref_source(ref: str, filename: Path) -> str | None:
    """Read *filename* from Git *ref*, returning ``None`` when it is new."""
    try:
        relative_path = filename.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return None
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, reads local Git object only.
        ["git", "show", f"{ref}:{relative_path}"],  # noqa: S607 - Git is a repository requirement.
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    return result.stdout if result.returncode == 0 else None


def unbaselined_violations(
    current: list[PrivateTestViolation], baseline: list[PrivateTestViolation]
) -> list[PrivateTestViolation]:
    """Return violations whose fingerprint count exceeds the Git baseline."""
    remaining = Counter(violation.fingerprint for violation in baseline)
    new: list[PrivateTestViolation] = []
    for violation in current:
        if remaining[violation.fingerprint]:
            remaining[violation.fingerprint] -= 1
        else:
            new.append(violation)
    return new


def is_test_file(file_path: Path) -> bool:
    """Return whether *file_path* belongs to either Pynchy test project."""
    path = file_path.as_posix()
    return "/tests/" in f"/{path}" or file_path.name.startswith("test_")


def main(argv: list[str]) -> int:
    """Run the private test-boundary check for filenames supplied by prek."""
    parser = argparse.ArgumentParser(
        description="Forbid tests crossing private first-party boundaries"
    )
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="First-party package name to guard (repeatable; overrides auto-detection)",
    )
    parser.add_argument(
        "--baseline-ref",
        help="temporarily tolerate matching violations already present in this Git ref",
    )
    parser.add_argument("filenames", nargs="*")
    args = parser.parse_args(argv)

    first_party = set(args.package) or detect_first_party_packages(Path.cwd())
    if not first_party:
        return 0

    violations: list[tuple[Path, PrivateTestViolation]] = []
    for filename in args.filenames:
        file_path = Path(filename)
        if file_path.suffix != ".py" or not is_test_file(file_path) or not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8")
        current = find_private_test_violations(content, first_party, str(file_path))
        if args.baseline_ref:
            baseline_source = _read_ref_source(args.baseline_ref, file_path)
            baseline = (
                find_private_test_violations(baseline_source, first_party, str(file_path))
                if baseline_source is not None
                else []
            )
            current = unbaselined_violations(current, baseline)
        violations.extend((file_path, violation) for violation in current)

    for file_path, violation in violations:
        print(
            f"{file_path}:{violation.line}: test {violation.kind.replace('-', ' ')} "
            f"'{violation.subject}' — drive a public entry point and assert observable behaviour; "
            f"use '# allow: {_ALLOW_MARKER} - external-process: "
            "<why this has no public observable>' only for an external-process side channel"
        )

    if violations:
        print(f"\nFound {len(violations)} new or changed private test-boundary violation(s).")
        if args.baseline_ref:
            print(
                f"Matching debt in {args.baseline_ref!r} is tolerated temporarily; remove each "
                "violation by rewriting its test around public behaviour."
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
