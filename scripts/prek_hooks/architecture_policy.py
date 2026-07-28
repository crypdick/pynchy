"""Policy loading and first-party package classification."""

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
class Role:
    name: str
    allowed: frozenset[str]
    violation_guidance: str


@dataclass(frozen=True)
class Package:
    name: str
    root: str
    role: str
    public_modules: frozenset[str]
    include_descendants: bool = True


@dataclass(frozen=True)
class PackageFamily:
    name: str
    root_pattern: str
    role: str
    public_modules: tuple[str, ...]


@dataclass(frozen=True)
class FileOverride:
    allow_unbounded_inbound_imports: bool


@dataclass(frozen=True)
class Policy:
    source_roots: tuple[SourceRoot, ...]
    composition_roots: frozenset[str]
    roles: tuple[Role, ...]
    packages: tuple[Package, ...]
    package_families: tuple[PackageFamily, ...]
    root_module_max_inbound_importers: int
    file_overrides: tuple[tuple[str, FileOverride], ...]
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
    if raw.get("version") != 2:
        raise ValueError("architecture policy version must be 2")
    root_module_max_inbound_importers = raw.get("root_module_max_inbound_importers")
    if (
        not isinstance(root_module_max_inbound_importers, int)
        or isinstance(root_module_max_inbound_importers, bool)
        or root_module_max_inbound_importers < 1
    ):
        raise ValueError("root_module_max_inbound_importers must be a positive integer")
    raw_file_overrides = raw.get("file_overrides", {})
    if not isinstance(raw_file_overrides, dict):
        raise TypeError("file_overrides must be a table of exact source-relative paths")
    file_overrides: list[tuple[str, FileOverride]] = []
    for source_path, item in raw_file_overrides.items():
        if not isinstance(item, dict) or set(item) != {"allow_unbounded_inbound_imports"}:
            raise ValueError(
                f"file override {source_path!r} must declare only allow_unbounded_inbound_imports"
            )
        if item["allow_unbounded_inbound_imports"] is not True:
            raise ValueError(
                f"file override {source_path!r} must set allow_unbounded_inbound_imports = true"
            )
        file_overrides.append((source_path, FileOverride(allow_unbounded_inbound_imports=True)))
    source_roots = tuple(
        SourceRoot(
            path=root / item["path"],
            module=item["module"],
            exclude=tuple(item.get("exclude", ())),
        )
        for item in raw.get("source_roots", ())
    )
    roles = tuple(
        Role(
            name=name,
            allowed=frozenset(item.get("allowed_dependencies", ())),
            violation_guidance=item.get("violation_guidance", "").strip(),
        )
        for name, item in raw.get("roles", {}).items()
    )
    packages = tuple(
        Package(
            name=item["name"],
            root=item["root"],
            role=item["role"],
            public_modules=frozenset(item["public_modules"]),
            include_descendants=item.get("include_descendants", True),
        )
        for item in raw.get("packages", ())
    )
    package_families = tuple(
        PackageFamily(
            name=item["name"],
            root_pattern=item["root_pattern"],
            role=item["role"],
            public_modules=tuple(item["public_modules"]),
        )
        for item in raw.get("package_families", ())
    )
    return Policy(
        source_roots=source_roots,
        composition_roots=frozenset(raw.get("composition_roots", ())),
        roles=roles,
        packages=packages,
        package_families=package_families,
        root_module_max_inbound_importers=root_module_max_inbound_importers,
        file_overrides=tuple(file_overrides),
        default_violation_guidance=raw["default_violation_guidance"].strip(),
    )


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


def _family_prefix(pattern: str) -> str:
    # Families stay one level deep. ``**`` would silently turn implementation
    # subpackages into peer architectural packages.
    if not pattern.endswith(".*") or "*" in pattern[:-2]:
        raise ValueError(f"package family root_pattern must end in one-level '.*': {pattern!r}")
    return pattern[:-2]


def resolve_packages(
    modules: dict[str, SourceModule],
    policy: Policy,
    policy_path: Path,
) -> tuple[tuple[Package, ...], list[Diagnostic]]:
    packages = list(policy.packages)
    diagnostics: list[Diagnostic] = []
    for family in policy.package_families:
        prefix = _family_prefix(family.root_pattern)
        family_roots = sorted(
            module.name
            for module in modules.values()
            if module.is_package
            and module.name.startswith(f"{prefix}.")
            and "." not in module.name[len(prefix) + 1 :]
        )
        if not family_roots:
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-policy",
                    f"package family {family.name!r} matches no first-party packages",
                )
            )
        for package_root in family_roots:
            try:
                public_modules = frozenset(
                    template.format(root=package_root) for template in family.public_modules
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    f"invalid public_modules template in package family {family.name!r}"
                ) from exc
            packages.append(
                Package(
                    name=f"{family.name}:{package_root.rsplit('.', 1)[-1]}",
                    root=package_root,
                    role=family.role,
                    public_modules=public_modules,
                )
            )

    roles = {role.name for role in policy.roles}
    known_modules = set(modules)
    seen_names: set[str] = set()
    seen_roots: set[str] = set()
    for package in packages:
        if package.name in seen_names:
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-policy",
                    f"duplicate package name {package.name!r}",
                )
            )
        if package.root in seen_roots:
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-policy",
                    f"duplicate package root {package.root!r}",
                )
            )
        if package.root not in known_modules:
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-policy",
                    f"package {package.name!r} has unknown root module {package.root!r}",
                )
            )
        if package.role not in roles:
            diagnostics.append(
                Diagnostic(
                    policy_path.as_posix(),
                    0,
                    "architecture-policy",
                    f"package {package.name!r} has unknown role {package.role!r}",
                )
            )
        for public_module in sorted(package.public_modules):
            if public_module != package.root and not public_module.startswith(f"{package.root}."):
                diagnostics.append(
                    Diagnostic(
                        policy_path.as_posix(),
                        0,
                        "architecture-policy",
                        (
                            f"package {package.name!r} exposes {public_module!r} "
                            f"outside its root {package.root!r}"
                        ),
                    )
                )
            elif public_module not in known_modules:
                diagnostics.append(
                    Diagnostic(
                        policy_path.as_posix(),
                        0,
                        "architecture-policy",
                        (f"package {package.name!r} exposes unknown module {public_module!r}"),
                    )
                )
        seen_names.add(package.name)
        seen_roots.add(package.root)
    return tuple(packages), diagnostics


def package_owns_module(package: Package, module: str) -> bool:
    return module == package.root or (
        package.include_descendants and module.startswith(f"{package.root}.")
    )


def owning_package(module: str, packages: tuple[Package, ...]) -> Package | None:
    matches = [package for package in packages if package_owns_module(package, module)]
    if not matches:
        return None
    return max(matches, key=lambda package: package.root.count("."))


def classify_modules(
    modules: dict[str, SourceModule],
    packages: tuple[Package, ...],
) -> tuple[dict[str, Package], list[Diagnostic]]:
    classified: dict[str, Package] = {}
    diagnostics: list[Diagnostic] = []
    for module in modules.values():
        package = owning_package(module.name, packages)
        if package is not None:
            classified[module.name] = package
            continue
        diagnostics.append(
            Diagnostic(
                module.path.as_posix(),
                0,
                "architecture-policy",
                f"first-party module {module.name!r} is unclassified; assign one package",
            )
        )
    return classified, diagnostics


def policy_cycle_diagnostics(policy: Policy, policy_path: Path) -> list[Diagnostic]:
    """Reject unknown role references and cycles in the positive graph."""
    roles = {role.name: role for role in policy.roles}
    diagnostics = [
        Diagnostic(
            policy_path.as_posix(),
            0,
            "architecture-policy",
            f"role {role.name!r} allows unknown role {name!r}",
        )
        for role in policy.roles
        for name in sorted(role.allowed - roles.keys())
    ]
    if len(roles) != len(policy.roles):
        diagnostics.append(
            Diagnostic(
                policy_path.as_posix(),
                0,
                "architecture-policy",
                "role names must be unique",
            )
        )
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
        if name in visited or name not in roles:
            return
        visiting.append(name)
        for target in sorted(roles[name].allowed - {name}):
            visit(target)
        visiting.pop()
        visited.add(name)

    for name in sorted(roles):
        visit(name)
    return diagnostics
