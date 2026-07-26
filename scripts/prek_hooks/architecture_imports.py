"""Whole-tree AST import extraction for the architecture boundary check."""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass

from scripts.prek_hooks.architecture_policy import (
    Component,
    Diagnostic,
    Policy,
    SourceModule,
    matching_components,
)


@dataclass(frozen=True)
class Dependency:
    importer: str
    imported: str
    target_component: str
    kind: str
    path: str
    line: int


def _resolve_from(module: SourceModule, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    relative_name = f"{'.' * node.level}{node.module or ''}"
    try:
        return importlib.util.resolve_name(relative_name, package)
    except (ImportError, ValueError):
        return None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _ImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        module: SourceModule,
        known_modules: set[str],
        first_party_roots: set[str],
    ) -> None:
        self.module = module
        self.known_modules = known_modules
        self.first_party_roots = first_party_roots
        self.dependencies: list[tuple[str, str, int]] = []
        self._type_checking_depth = 0
        self._import_module_names: set[str] = set()
        self._importlib_names: set[str] = set()

    @property
    def _kind(self) -> str:
        return "type-checking-import" if self._type_checking_depth else "runtime-import"

    def _is_known(self, module: str) -> bool:
        return module in self.known_modules or any(
            name.startswith(f"{module}.") for name in self.known_modules
        )

    def _add(self, imported: str | None, line: int) -> None:
        if imported and imported.split(".", 1)[0] in self.first_party_roots:
            self.dependencies.append((imported, self._kind, line))

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self.visit(node.test)
        self._type_checking_depth += 1
        for statement in node.body:
            self.visit(statement)
        self._type_checking_depth -= 1
        for statement in node.orelse:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node.lineno)
            if alias.name == "importlib":
                self._importlib_names.add(alias.asname or "importlib")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_from(self.module, node)
        if base == "importlib":
            self._import_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )
        targets = {base} if base else set()
        if base:
            submodules = {
                f"{base}.{alias.name}"
                for alias in node.names
                if alias.name != "*" and self._is_known(f"{base}.{alias.name}")
            }
            if submodules:
                targets = submodules
        for target in sorted(targets):
            self._add(target, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        is_loader = isinstance(node.func, ast.Name) and node.func.id in {
            "__import__",
            *self._import_module_names,
        }
        if isinstance(node.func, ast.Attribute):
            is_loader = (
                node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in self._importlib_names
            )
        if is_loader and node.args:
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                self._add(target.value, node.lineno)
        self.generic_visit(node)


def collect_dependencies(
    modules: dict[str, SourceModule],
    classified: dict[str, Component],
    policy: Policy,
) -> tuple[list[Dependency], list[Diagnostic]]:
    dependencies: list[Dependency] = []
    diagnostics: list[Diagnostic] = []
    first_party_roots = {item.module.split(".", 1)[0] for item in policy.source_roots}
    for module in modules.values():
        if module.name not in classified:
            continue
        try:
            tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
        except (SyntaxError, ValueError) as exc:
            diagnostics.append(
                Diagnostic(module.path.as_posix(), 0, "architecture-parse", str(exc))
            )
            continue
        visitor = _ImportVisitor(module, set(modules), first_party_roots)
        visitor.visit(tree)
        for imported, kind, line in visitor.dependencies:
            target_matches = matching_components(imported, policy)
            if len(target_matches) != 1:
                diagnostics.append(
                    Diagnostic(
                        module.path.as_posix(),
                        line,
                        "architecture-import",
                        (
                            f"first-party import {imported!r} resolves to "
                            f"{len(target_matches)} components"
                        ),
                    )
                )
                continue
            dependencies.append(
                Dependency(
                    importer=module.name,
                    imported=imported,
                    target_component=target_matches[0].name,
                    kind=kind,
                    path=module.path.as_posix(),
                    line=line,
                )
            )
    return dependencies, diagnostics


def invalid_dependencies(
    dependencies: list[Dependency],
    classified: dict[str, Component],
    policy: Policy,
) -> list[Dependency]:
    return sorted(
        (
            dependency
            for dependency in dependencies
            if dependency.importer not in policy.composition_roots
            and dependency.target_component != classified[dependency.importer].name
            and dependency.target_component not in classified[dependency.importer].allowed
        ),
        key=lambda item: (item.path, item.line, item.imported, item.kind),
    )
