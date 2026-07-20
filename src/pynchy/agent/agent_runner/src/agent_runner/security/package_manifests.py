"""Parse supported dependency manifests and lockfiles at write boundaries."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import PurePosixPath
from urllib.parse import urlparse

from agent_runner.security.packages import (
    PackageEcosystem,
    PackageIntent,
    PackageReference,
    PackageSource,
    classify_package_source,
    normalize_package_name,
    parse_npm_coordinate,
    parse_package_spec,
    unique_package_references,
)

_MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements.in",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "cargo.toml",
        "cargo.lock",
    }
)


def parse_manifest_write(path: str, contents: tuple[str, ...]) -> tuple[PackageReference, ...]:
    """Extract package coordinates from a manifest or lockfile write."""
    name = PurePosixPath(path.replace("\\", "/").casefold()).name
    if name not in _MANIFEST_NAMES:
        return ()
    references: list[PackageReference] = []
    for content in contents:
        manifest_text = (
            _strip_patch_markers(content)
            if "*** Begin Patch" in content or re.search(r"^@@", content, re.MULTILINE)
            else content
        )
        references.extend(_parse_manifest_content(name, manifest_text))
    return unique_package_references(references)


def is_package_manifest(path: str) -> bool:
    """Return whether a path names a supported package manifest or lockfile."""
    return PurePosixPath(path.replace("\\", "/").casefold()).name in _MANIFEST_NAMES


def _parse_manifest_content(name: str, content: str) -> list[PackageReference]:
    if name.startswith("requirements"):
        return _requirements(content)
    if name in {"pyproject.toml", "uv.lock", "cargo.toml", "cargo.lock"}:
        return _toml_manifest(name, content)
    if name in {"package.json", "package-lock.json", "npm-shrinkwrap.json"}:
        return _json_manifest(name, content)
    if name == "yarn.lock":
        return _yarn_lock(content)
    return []


def _requirements(content: str) -> list[PackageReference]:
    refs: list[PackageReference] = []
    custom_registry = any(
        line.strip().startswith(
            ("--index-url", "--extra-index-url", "--find-links", "-f", "--no-index")
        )
        for line in content.splitlines()
    )
    for line in content.splitlines():
        spec = line.strip()
        if not spec or spec.startswith(("#", "-")):
            continue
        ref = parse_package_spec(
            PackageEcosystem.PYPI,
            spec,
            intent=PackageIntent.RECONCILIATION,
        )
        locked = _with_lock(ref, lock_pinned=ref.version is not None)
        refs.append(
            _with_source(locked, PackageSource.CUSTOM_REGISTRY)
            if custom_registry and locked.source is PackageSource.REGISTRY
            else locked
        )
    return refs


def _toml_manifest(name: str, content: str) -> list[PackageReference]:
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return _partial_manifest_specs(name, content)
    if name == "pyproject.toml":
        return _pyproject_manifest(parsed)
    packages = parsed.get("package", [])
    if name == "uv.lock" and isinstance(packages, list):
        return [
            PackageReference(
                PackageEcosystem.PYPI,
                normalize_package_name(PackageEcosystem.PYPI, str(item.get("name"))),
                str(item.get("version")),
                _uv_lock_source(item.get("source")),
                PackageIntent.RECONCILIATION,
                lock_pinned=True,
            )
            for item in packages
            if isinstance(item, dict) and item.get("name") and item.get("version")
        ]
    if name == "cargo.lock" and isinstance(packages, list):
        return [
            PackageReference(
                PackageEcosystem.CARGO,
                str(item.get("name")),
                str(item.get("version")),
                _cargo_lock_source(item.get("source")),
                PackageIntent.RECONCILIATION,
                lock_pinned=True,
            )
            for item in packages
            if isinstance(item, dict) and item.get("name") and item.get("version")
        ]
    dependencies = parsed.get("dependencies", {})
    if name == "cargo.toml" and isinstance(dependencies, dict):
        return [_cargo_manifest_ref(pkg, value) for pkg, value in dependencies.items()]
    return []


def _pyproject_manifest(parsed: dict[str, object]) -> list[PackageReference]:
    project = parsed.get("project", {})
    specs = list(project.get("dependencies", [])) if isinstance(project, dict) else []
    optional = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                specs.extend(values)
    references = [
        parse_package_spec(
            PackageEcosystem.PYPI,
            str(spec),
            intent=PackageIntent.RECONCILIATION,
        )
        for spec in specs
    ]
    tool = parsed.get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    custom_registry = isinstance(uv, dict) and bool(uv.get("index") or uv.get("sources"))
    if not custom_registry:
        return references
    return [
        _with_source(reference, PackageSource.CUSTOM_REGISTRY)
        if reference.source is PackageSource.REGISTRY
        else reference
        for reference in references
    ]


def _cargo_manifest_ref(name: str, value: object) -> PackageReference:
    version: str | None = value if isinstance(value, str) else None
    source = PackageSource.REGISTRY
    if isinstance(value, dict):
        version_value = value.get("version")
        version = version_value if isinstance(version_value, str) else None
        if "git" in value:
            source = PackageSource.VCS
        elif "path" in value:
            source = PackageSource.LOCAL
    return PackageReference(
        PackageEcosystem.CARGO,
        name,
        version,
        source,
        PackageIntent.RECONCILIATION,
    )


def _json_manifest(name: str, content: str) -> list[PackageReference]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _partial_manifest_specs(name, content)
    if not isinstance(parsed, dict):
        return []
    if name == "package.json":
        refs: list[PackageReference] = []
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            values = parsed.get(section, {})
            if isinstance(values, dict):
                refs.extend(
                    _npm_manifest_ref(package, value, lock=False)
                    for package, value in values.items()
                )
        return refs
    packages = parsed.get("packages", {})
    if not isinstance(packages, dict):
        return []
    return [
        _npm_manifest_ref(path.removeprefix("node_modules/"), value, lock=True)
        for path, value in packages.items()
        if path and isinstance(value, dict) and value.get("version")
    ]


def _npm_manifest_ref(name: str, value: object, *, lock: bool) -> PackageReference:
    version = value.get("version") if isinstance(value, dict) else value
    version_text = version if isinstance(version, str) else None
    source = _npm_lock_source(value) if lock else classify_package_source(version_text or "")
    exact_pattern = r"\d+(?:\.\d+){1,3}(?:[-+].+)?"
    exact = (
        version_text
        if lock or (version_text and re.fullmatch(exact_pattern, version_text))
        else None
    )
    return PackageReference(
        PackageEcosystem.NPM,
        normalize_package_name(PackageEcosystem.NPM, name),
        exact,
        source,
        PackageIntent.RECONCILIATION,
        lock,
    )


def _yarn_lock(content: str) -> list[PackageReference]:
    refs: list[PackageReference] = []
    current_name: str | None = None
    current_version: str | None = None
    current_source = PackageSource.CUSTOM_REGISTRY
    for line in (*content.splitlines(), "__END__:"):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            if current_name and current_version:
                refs.append(
                    PackageReference(
                        PackageEcosystem.NPM,
                        current_name,
                        current_version,
                        current_source,
                        PackageIntent.RECONCILIATION,
                        lock_pinned=True,
                    )
                )
            current_name, _version = parse_npm_coordinate(
                line.rstrip(":").split(",", 1)[0].strip('"')
            )
            current_version = None
            current_source = PackageSource.CUSTOM_REGISTRY
        elif current_name and (match := re.match(r'\s+version\s+"([^"]+)"', line)):
            current_version = match.group(1)
        elif current_name and (match := re.match(r'\s+resolved\s+"([^"]+)"', line)):
            current_source = _registry_url_source(PackageEcosystem.NPM, match.group(1))
        elif current_name and "resolution:" in line and "@npm:" in line:
            current_source = PackageSource.REGISTRY
    return refs


def _uv_lock_source(value: object) -> PackageSource:
    if not isinstance(value, dict):
        return PackageSource.CUSTOM_REGISTRY
    registry = value.get("registry")
    if isinstance(registry, str):
        return _registry_url_source(PackageEcosystem.PYPI, registry)
    if "git" in value:
        return PackageSource.VCS
    if "url" in value:
        return PackageSource.DIRECT_URL
    if any(key in value for key in ("path", "editable", "virtual")):
        return PackageSource.LOCAL
    return PackageSource.CUSTOM_REGISTRY


def _cargo_lock_source(value: object) -> PackageSource:
    if not isinstance(value, str):
        return PackageSource.LOCAL
    if value.startswith("git+"):
        return PackageSource.VCS
    if value.startswith("registry+"):
        registry = value.removeprefix("registry+").split("#", 1)[0]
        return _registry_url_source(PackageEcosystem.CARGO, registry)
    return PackageSource.CUSTOM_REGISTRY


def _npm_lock_source(value: object) -> PackageSource:
    if not isinstance(value, dict):
        return PackageSource.CUSTOM_REGISTRY
    resolved = value.get("resolved")
    if not isinstance(resolved, str):
        return PackageSource.CUSTOM_REGISTRY
    classified = classify_package_source(resolved)
    if classified is PackageSource.DIRECT_URL:
        return _registry_url_source(PackageEcosystem.NPM, resolved)
    return classified


def _registry_url_source(ecosystem: PackageEcosystem, value: str) -> PackageSource:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    canonical_hosts = {
        PackageEcosystem.PYPI: {"pypi.org"},
        PackageEcosystem.NPM: {"registry.npmjs.org", "registry.yarnpkg.com"},
        PackageEcosystem.CARGO: {"github.com", "index.crates.io"},
    }[ecosystem]
    if (
        parsed.scheme == "https"
        and hostname in canonical_hosts
        and (
            ecosystem is not PackageEcosystem.CARGO
            or hostname != "github.com"
            or parsed.path == "/rust-lang/crates.io-index"
        )
    ):
        return PackageSource.REGISTRY
    return PackageSource.CUSTOM_REGISTRY


def _partial_manifest_specs(name: str, content: str) -> list[PackageReference]:
    ecosystem = (
        PackageEcosystem.NPM
        if "package" in name or name == "yarn.lock"
        else (PackageEcosystem.CARGO if name.startswith("cargo") else PackageEcosystem.PYPI)
    )
    pattern = r'["\']([@A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?(?:==|@)[^"\'\s,]+)["\']'
    return [
        parse_package_spec(ecosystem, match.group(1), intent=PackageIntent.RECONCILIATION)
        for match in re.finditer(pattern, content)
    ]


def _strip_patch_markers(content: str) -> str:
    return "\n".join(
        line[1:] if line.startswith("+") and not line.startswith("+++") else line
        for line in content.splitlines()
        if not line.startswith("-") or line.startswith("---")
    )


def _with_lock(reference: PackageReference, *, lock_pinned: bool) -> PackageReference:
    return PackageReference(
        reference.ecosystem,
        reference.name,
        reference.version,
        reference.source,
        reference.intent,
        lock_pinned,
    )


def _with_source(reference: PackageReference, source: PackageSource) -> PackageReference:
    return PackageReference(
        reference.ecosystem,
        reference.name,
        reference.version,
        source,
        reference.intent,
        reference.lock_pinned,
    )
