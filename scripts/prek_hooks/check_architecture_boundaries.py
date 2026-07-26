#!/usr/bin/env python3
"""Enforce a positive, ratcheted first-party dependency policy."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.prek_hooks.architecture_imports import (
    Dependency,
    collect_dependencies,
    invalid_dependencies,
)
from scripts.prek_hooks.architecture_policy import (
    Component,
    Diagnostic,
    classify_modules,
    discover_modules,
    load_policy,
    policy_cycle_diagnostics,
)


@dataclass(frozen=True)
class BaselineEntry:
    importer: str
    target_component: str
    kind: str
    count: int
    imports: tuple[str, ...]
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.importer, self.target_component, self.kind)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_baseline(path: Path) -> tuple[BaselineEntry, ...]:
    raw = _load_toml(path)
    if raw.get("version") != 1:
        raise ValueError("architecture baseline version must be 1")
    return tuple(
        BaselineEntry(
            importer=item["importer"],
            target_component=item["target_component"],
            kind=item["kind"],
            count=item["count"],
            imports=tuple(item["imports"]),
            reason=item.get("reason", "").strip(),
        )
        for item in raw.get("violations", ())
    )


def _boundary_message(
    dependency: Dependency,
    importer_component: Component,
    target_component: Component,
    default_guidance: str,
) -> str:
    allowed = ", ".join(sorted(importer_component.allowed | {importer_component.name}))
    direction = target_component.violation_guidance or default_guidance
    return (
        f"{dependency.importer!r} ({importer_component.name}) imports {dependency.imported!r} "
        f"({dependency.target_component}) via {dependency.kind}; allowed components: {allowed}. "
        f"To fix: {direction}"
    )


def _validate_baseline(
    baseline: tuple[BaselineEntry, ...],
    baseline_path: Path,
) -> tuple[dict[tuple[str, str, str], BaselineEntry], list[Diagnostic]]:
    entries: dict[tuple[str, str, str], BaselineEntry] = {}
    diagnostics: list[Diagnostic] = []
    for entry in baseline:
        if entry.key in entries:
            diagnostics.append(
                Diagnostic(
                    baseline_path.as_posix(),
                    0,
                    "architecture-baseline",
                    f"duplicate baseline entry {entry.key!r}",
                )
            )
        if entry.count < 1 or entry.count != len(entry.imports) or not entry.reason:
            diagnostics.append(
                Diagnostic(
                    baseline_path.as_posix(),
                    0,
                    "architecture-baseline",
                    (
                        f"entry {entry.key!r} needs a positive count matching imports "
                        "and a nonempty reason"
                    ),
                )
            )
        entries[entry.key] = entry
    return entries, diagnostics


def compare_baseline(
    current: list[Dependency],
    baseline: tuple[BaselineEntry, ...],
    classified: dict[str, Component],
    components: dict[str, Component],
    default_guidance: str,
    baseline_path: Path,
) -> list[Diagnostic]:
    entries, diagnostics = _validate_baseline(baseline, baseline_path)
    grouped: dict[tuple[str, str, str], list[Dependency]] = defaultdict(list)
    for dependency in current:
        key = (dependency.importer, dependency.target_component, dependency.kind)
        grouped[key].append(dependency)

    for key, dependencies in grouped.items():
        expected = entries.get(key)
        remaining: defaultdict[str, int] = defaultdict(int)
        if expected:
            for imported in expected.imports:
                remaining[imported] += 1
        new_dependencies: list[Dependency] = []
        for dependency in dependencies:
            if remaining[dependency.imported]:
                remaining[dependency.imported] -= 1
            else:
                new_dependencies.append(dependency)
        diagnostics.extend(
            Diagnostic(
                dependency.path,
                dependency.line,
                "architecture-boundary",
                _boundary_message(
                    dependency,
                    classified[dependency.importer],
                    components[dependency.target_component],
                    default_guidance,
                ),
            )
            for dependency in new_dependencies
        )
        missing_count = sum(remaining.values())
        if expected and missing_count:
            diagnostics.append(
                Diagnostic(
                    baseline_path.as_posix(),
                    0,
                    "architecture-baseline-stale",
                    (
                        f"{key!r} has {missing_count} recorded import(s) no longer in source; "
                        "shrink the entry"
                    ),
                )
            )
    diagnostics.extend(
        Diagnostic(
            baseline_path.as_posix(),
            0,
            "architecture-baseline-stale",
            f"{key!r} no longer matches source; remove it",
        )
        for key in entries.keys() - grouped.keys()
    )
    return diagnostics


def check_architecture(
    root: Path,
    policy_path: Path,
    baseline_path: Path,
) -> tuple[list[Diagnostic], list[Dependency]]:
    policy = load_policy(root, policy_path)
    modules = discover_modules(policy)
    classified, diagnostics = classify_modules(modules, policy)
    dependencies, import_diagnostics = collect_dependencies(modules, classified, policy)
    diagnostics.extend(import_diagnostics)
    diagnostics.extend(policy_cycle_diagnostics(policy, policy_path))
    current = invalid_dependencies(dependencies, classified, policy)
    components = {component.name: component for component in policy.components}
    diagnostics.extend(
        compare_baseline(
            current,
            load_baseline(baseline_path),
            classified,
            components,
            policy.default_violation_guidance,
            baseline_path,
        )
    )
    normalized = {
        Diagnostic(
            Path(diagnostic.path).relative_to(root).as_posix(),
            diagnostic.line,
            diagnostic.code,
            diagnostic.message,
        )
        if Path(diagnostic.path).is_relative_to(root)
        else diagnostic
        for diagnostic in diagnostics
    }
    return sorted(normalized), current


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="architecture.toml")
    parser.add_argument("--baseline", default="architecture-baseline.toml")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        diagnostics, _current = check_architecture(
            root,
            root / args.policy,
            root / args.baseline,
        )
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"architecture configuration error: {exc}")
        return 2
    for diagnostic in diagnostics:
        print(diagnostic.render())
    if diagnostics:
        print(f"\nFound {len(diagnostics)} architecture policy problem(s).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
