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
    Violation,
    collect_dependencies,
    dependency_violations,
)
from scripts.prek_hooks.architecture_policy import (
    Diagnostic,
    Package,
    Role,
    classify_modules,
    discover_modules,
    load_policy,
    policy_cycle_diagnostics,
    resolve_packages,
)


@dataclass(frozen=True)
class BaselineEntry:
    importer: str
    target_role: str
    rule: str
    kind: str
    count: int
    imports: tuple[str, ...]
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.importer, self.target_role, self.rule, self.kind)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_baseline(path: Path) -> tuple[BaselineEntry, ...]:
    raw = _load_toml(path)
    if raw.get("version") != 2:
        raise ValueError("architecture baseline version must be 2")
    return tuple(
        BaselineEntry(
            importer=item["importer"],
            target_role=item["target_role"],
            rule=item["rule"],
            kind=item["kind"],
            count=item["count"],
            imports=tuple(item["imports"]),
            reason=item.get("reason", "").strip(),
        )
        for item in raw.get("violations", ())
    )


def _boundary_message(
    violation: Violation,
    importer_package: Package,
    target_package: Package,
    importer_role: Role,
    target_role: Role,
    default_guidance: str,
) -> str:
    if violation.rule == "visibility":
        public = ", ".join(sorted(target_package.public_modules)) or "(none)"
        return (
            f"{violation.importer!r} ({importer_package.root}) imports private module "
            f"{violation.imported!r} from package {target_package.root!r} via "
            f"{violation.kind}; declared public modules: {public}. "
            "To fix: import the package's declared facade, or expose this exact module "
            "only when it is intentionally public."
        )
    allowed = ", ".join(sorted(importer_role.allowed)) or "(none)"
    direction = target_role.violation_guidance or default_guidance
    return (
        f"{violation.importer!r} ({importer_role.name}) imports {violation.imported!r} "
        f"({violation.target_role}) via {violation.kind}; allowed roles: {allowed}. "
        f"To fix: {direction}"
    )


def _validate_baseline(
    baseline: tuple[BaselineEntry, ...],
    baseline_path: Path,
) -> tuple[dict[tuple[str, str, str, str], BaselineEntry], list[Diagnostic]]:
    entries: dict[tuple[str, str, str, str], BaselineEntry] = {}
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
        if (
            entry.rule not in {"direction", "visibility"}
            or entry.count < 1
            or entry.count != len(entry.imports)
            or not entry.reason
        ):
            diagnostics.append(
                Diagnostic(
                    baseline_path.as_posix(),
                    0,
                    "architecture-baseline",
                    (
                        f"entry {entry.key!r} needs a direction or visibility rule, "
                        "a positive count matching imports, and a nonempty reason"
                    ),
                )
            )
        entries[entry.key] = entry
    return entries, diagnostics


def compare_baseline(
    current: list[Violation],
    baseline: tuple[BaselineEntry, ...],
    classified: dict[str, Package],
    packages: tuple[Package, ...],
    roles: tuple[Role, ...],
    default_guidance: str,
    baseline_path: Path,
) -> list[Diagnostic]:
    entries, diagnostics = _validate_baseline(baseline, baseline_path)
    grouped: dict[tuple[str, str, str, str], list[Violation]] = defaultdict(list)
    for violation in current:
        key = (
            violation.importer,
            violation.target_role,
            violation.rule,
            violation.kind,
        )
        grouped[key].append(violation)
    packages_by_root = {package.root: package for package in packages}
    roles_by_name = {role.name: role for role in roles}

    for key, violations in grouped.items():
        expected = entries.get(key)
        remaining: defaultdict[str, int] = defaultdict(int)
        if expected:
            for imported in expected.imports:
                remaining[imported] += 1
        new_violations: list[Violation] = []
        for violation in violations:
            if remaining[violation.imported]:
                remaining[violation.imported] -= 1
            else:
                new_violations.append(violation)
        diagnostics.extend(
            Diagnostic(
                violation.path,
                violation.line,
                f"architecture-{violation.rule}",
                _boundary_message(
                    violation,
                    classified[violation.importer],
                    packages_by_root[violation.target_package],
                    roles_by_name[classified[violation.importer].role],
                    roles_by_name[violation.target_role],
                    default_guidance,
                ),
            )
            for violation in new_violations
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
) -> tuple[list[Diagnostic], list[Violation]]:
    policy = load_policy(root, policy_path)
    modules = discover_modules(policy)
    packages, diagnostics = resolve_packages(modules, policy, policy_path)
    classified, classification_diagnostics = classify_modules(modules, packages)
    diagnostics.extend(classification_diagnostics)
    dependencies, import_diagnostics = collect_dependencies(
        modules,
        classified,
        packages,
        policy,
    )
    diagnostics.extend(import_diagnostics)
    diagnostics.extend(policy_cycle_diagnostics(policy, policy_path))
    current = dependency_violations(dependencies, classified, packages, policy)
    diagnostics.extend(
        compare_baseline(
            current,
            load_baseline(baseline_path),
            classified,
            packages,
            policy.roles,
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
