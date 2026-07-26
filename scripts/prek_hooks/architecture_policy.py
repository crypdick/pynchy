"""Policy loading and first-party component classification."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SourceRoot:
    path: Path
    module: str
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Component:
    name: str
    patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    allowed: frozenset[str]
    violation_guidance: str


@dataclass(frozen=True)
class Policy:
    source_roots: tuple[SourceRoot, ...]
    composition_roots: frozenset[str]
    components: tuple[Component, ...]
    default_violation_guidance: str


@dataclass(frozen=True)
class SourceModule:
    name: str
    path: Path
    is_package: bool


@dataclass(frozen=True, order=True)
class Diagnostic:
    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.code}: {self.message}"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_policy(root: Path, path: Path) -> Policy:
    raw = _load_toml(path)
    if raw.get("version") != 1:
        raise ValueError("architecture policy version must be 1")
    source_roots = tuple(
        SourceRoot(
            path=root / item["path"],
            module=item["module"],
            exclude=tuple(item.get("exclude", ())),
        )
        for item in raw.get("source_roots", ())
    )
    components = tuple(
        Component(
            name=item["name"],
            patterns=tuple(item["module_patterns"]),
            exclude_patterns=tuple(item.get("exclude_patterns", ())),
            allowed=frozenset(item.get("allowed_dependencies", ())),
            violation_guidance=item.get("violation_guidance", "").strip(),
        )
        for item in raw.get("components", ())
    )
    return Policy(
        source_roots=source_roots,
        composition_roots=frozenset(raw.get("composition_roots", ())),
        components=components,
        default_violation_guidance=raw["default_violation_guidance"].strip(),
    )


def module_matches_pattern(module: str, pattern: str) -> bool:
    """Match an exact, one-level ``.*``, or recursive ``.**`` module pattern."""
    if pattern.endswith(".**"):
        prefix = pattern[:-3]
        return module == prefix or module.startswith(f"{prefix}.")
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        if not module.startswith(f"{prefix}."):
            return False
        return "." not in module[len(prefix) + 1 :]
    return module == pattern


def matching_components(module: str, policy: Policy) -> list[Component]:
    return [
        component
        for component in policy.components
        if any(module_matches_pattern(module, pattern) for pattern in component.patterns)
        and not any(
            module_matches_pattern(module, pattern) for pattern in component.exclude_patterns
        )
    ]


def _excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        relative_path == pattern.removesuffix("/**")
        or (pattern.endswith("/**") and relative_path.startswith(f"{pattern.removesuffix('/**')}/"))
        for pattern in patterns
    )


def discover_modules(policy: Policy) -> dict[str, SourceModule]:
    modules: dict[str, SourceModule] = {}
    for source_root in policy.source_roots:
        for path in sorted(source_root.path.rglob("*.py")):
            relative = path.relative_to(source_root.path)
            if _excluded(relative.as_posix(), source_root.exclude):
                continue
            is_package = path.name == "__init__.py"
            parts = relative.parent.parts if is_package else relative.with_suffix("").parts
            suffix = ".".join(parts)
            name = source_root.module if not suffix else f"{source_root.module}.{suffix}"
            if name in modules:
                raise ValueError(f"duplicate first-party module {name!r}")
            modules[name] = SourceModule(name=name, path=path, is_package=is_package)
    return modules


def classify_modules(
    modules: dict[str, SourceModule], policy: Policy
) -> tuple[dict[str, Component], list[Diagnostic]]:
    classified: dict[str, Component] = {}
    diagnostics: list[Diagnostic] = []
    for module in modules.values():
        matches = matching_components(module.name, policy)
        if len(matches) == 1:
            classified[module.name] = matches[0]
            continue
        problem = "unclassified" if not matches else f"matched {', '.join(c.name for c in matches)}"
        diagnostics.append(
            Diagnostic(
                module.path.as_posix(),
                0,
                "architecture-policy",
                f"first-party module {module.name!r} is {problem}; assign exactly one component",
            )
        )
    return classified, diagnostics


def policy_cycle_diagnostics(policy: Policy, policy_path: Path) -> list[Diagnostic]:
    """Reject unknown component references and cycles in the positive graph."""
    components = {component.name: component for component in policy.components}
    diagnostics = [
        Diagnostic(
            policy_path.as_posix(),
            0,
            "architecture-policy",
            f"component {component.name!r} allows unknown component {name!r}",
        )
        for component in policy.components
        for name in sorted(component.allowed - components.keys())
    ]
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            cycle = [*visiting[visiting.index(name) :], name]
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-cycle",
                    f"allowed dependency graph contains {' -> '.join(cycle)}",
                )
            )
            return
        if name in visited or name not in components:
            return
        visiting.append(name)
        for target in sorted(components[name].allowed - {name}):
            visit(target)
        visiting.pop()
        visited.add(name)

    for name in sorted(components):
        visit(name)
    return diagnostics
