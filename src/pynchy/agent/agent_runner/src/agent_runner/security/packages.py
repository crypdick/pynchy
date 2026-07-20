"""Parse package operations without sending command or workspace content upstream."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum


class PackageEcosystem(StrEnum):
    """Authoritative registry namespace for a package."""

    PYPI = "pypi"
    NPM = "npm"
    CARGO = "cargo"


class PackageSource(StrEnum):
    """How the requested package would be resolved."""

    REGISTRY = "registry"
    DIRECT_URL = "direct_url"
    VCS = "vcs"
    LOCAL = "local"
    SHELL = "shell"
    AMBIGUOUS = "ambiguous"
    CUSTOM_REGISTRY = "custom_registry"


class PackageIntent(StrEnum):
    """Execution context for a package reference."""

    DEPENDENCY = "dependency"
    EXECUTABLE = "executable"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class PackageReference:
    """Normalized package coordinate retained at the agent boundary."""

    ecosystem: PackageEcosystem
    name: str | None
    version: str | None
    source: PackageSource
    intent: PackageIntent
    lock_pinned: bool = False

    def to_wire(self) -> dict[str, str | bool | None]:
        """Serialize only the normalized coordinate and policy attributes."""
        return {
            "ecosystem": self.ecosystem.value,
            "name": self.name,
            "version": self.version,
            "source": self.source.value,
            "intent": self.intent.value,
            "lock_pinned": self.lock_pinned,
        }


_PACKAGE_COMMAND = re.compile(
    r"(?:^|[;&|]\s*|\s)(?P<manager>uv\s+tool\s+install|uv\s+add|uvx|"
    r"pipx?\s+install|npm\s+(?:install|i)|yarn\s+add|cargo\s+install)"
    r"(?:\s+(?P<arguments>[^;&|]*))?",
    re.IGNORECASE,
)
_PYTHON_EXACT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?={2,3}(?P<version>[^=]+)$")
_PYTHON_NAME = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?(?:[<>=!~].*)?$")
_CARGO_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_PYTHON_AT_EXACT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?@(?P<version>[A-Za-z0-9_.+!-]+)$"
)
_CUSTOM_REGISTRY_ENV = re.compile(
    r"(?:^|\s)(?:PIP_INDEX_URL|PIP_EXTRA_INDEX_URL|UV_INDEX_URL|UV_DEFAULT_INDEX|"
    r"UV_EXTRA_INDEX_URL|PIP_FIND_LINKS|PIP_NO_INDEX|UV_FIND_LINKS|UV_NO_INDEX|"
    r"NPM_CONFIG_REGISTRY|CARGO_REGISTRIES_[A-Z0-9_]+_INDEX)="
)
_CUSTOM_REGISTRY_OPTIONS = frozenset(
    {
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--registry",
        "--find-links",
        "-f",
        "--no-index",
    }
)
_OPTIONS_WITH_VALUE = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "--index-url",
        "--extra-index-url",
        "--index",
        "--default-index",
        "--find-links",
        "-f",
        "--python",
        "--directory",
        "--package",
        "--registry",
        "--root",
        "--target",
    }
)


def parse_package_commands(command: str) -> tuple[PackageReference, ...]:
    """Extract typed references from supported package-manager commands."""
    references: list[PackageReference] = []
    for match in _PACKAGE_COMMAND.finditer(command):
        manager = " ".join(match.group("manager").casefold().split())
        arguments = match.group("arguments") or ""
        command_prefix = re.split(r"[;&|]", command[: match.start("manager")])[-1]
        references.extend(
            _parse_command_match(
                manager,
                arguments,
                custom_registry_env=_CUSTOM_REGISTRY_ENV.search(command_prefix) is not None,
            )
        )
    return unique_package_references(references)


def _parse_command_match(
    manager: str,
    arguments: str,
    *,
    custom_registry_env: bool,
) -> list[PackageReference]:
    ecosystem = _ecosystem(manager)
    intent = (
        PackageIntent.EXECUTABLE
        if manager in {"uv tool install", "uvx", "pipx install", "cargo install"}
        else PackageIntent.DEPENDENCY
    )
    try:
        tokens = shlex.split(arguments)
    except ValueError:
        return [_ambiguous(ecosystem, intent)]
    if any(_shell_evaluated(token) for token in tokens):
        return [PackageReference(ecosystem, None, None, PackageSource.SHELL, intent)]
    if manager.startswith("npm") and any(item in {"-g", "--global"} for item in tokens):
        intent = PackageIntent.EXECUTABLE
    cargo_version = (
        _option_value(tokens, "--version") if ecosystem is PackageEcosystem.CARGO else None
    )
    specs = _package_specs(tokens, cargo=ecosystem is PackageEcosystem.CARGO)
    if not specs:
        return [_ambiguous(ecosystem, intent)]
    if ecosystem is PackageEcosystem.PYPI and "@" in specs:
        marker = specs.index("@")
        if marker > 0 and marker + 1 < len(specs):
            specs = [f"{specs[marker - 1]} @ {specs[marker + 1]}"]
    references = [
        parse_package_spec(
            ecosystem,
            spec,
            intent=intent,
            cargo_version=cargo_version,
        )
        for spec in specs
    ]
    if custom_registry_env or _has_custom_registry_option(tokens):
        references = [
            _with_source(reference, PackageSource.CUSTOM_REGISTRY)
            if reference.source is PackageSource.REGISTRY
            else reference
            for reference in references
        ]
    return references[:1] if intent is PackageIntent.EXECUTABLE else references


def _package_specs(tokens: list[str], *, cargo: bool) -> list[str]:
    specs: list[str] = []
    skip_next = False
    for item in tokens:
        if skip_next:
            skip_next = False
            continue
        if item in _OPTIONS_WITH_VALUE or (cargo and item == "--version"):
            skip_next = True
            continue
        if item == "--":
            continue
        if item.startswith("-"):
            continue
        specs.append(item)
    return specs


def parse_package_spec(
    ecosystem: PackageEcosystem,
    spec: str,
    *,
    intent: PackageIntent,
    cargo_version: str | None = None,
    lock_pinned: bool = False,
) -> PackageReference:
    stripped = spec.strip().strip("'\"")
    source = classify_package_source(stripped)
    if source is not PackageSource.REGISTRY:
        return PackageReference(
            ecosystem,
            _direct_name(stripped),
            None,
            source,
            intent,
            lock_pinned,
        )
    if ecosystem is PackageEcosystem.NPM:
        name, version = parse_npm_coordinate(stripped)
    elif ecosystem is PackageEcosystem.CARGO:
        name = stripped if _CARGO_NAME.fullmatch(stripped) else None
        version = cargo_version
    else:
        exact = _PYTHON_EXACT.fullmatch(stripped) or (
            _PYTHON_AT_EXACT.fullmatch(stripped) if intent is PackageIntent.EXECUTABLE else None
        )
        match = exact or _PYTHON_NAME.fullmatch(stripped)
        name = match.group("name") if match else None
        version = exact.group("version") if exact else None
    return PackageReference(
        ecosystem,
        normalize_package_name(ecosystem, name),
        version,
        PackageSource.REGISTRY if name else PackageSource.AMBIGUOUS,
        intent,
        lock_pinned,
    )


def _ecosystem(manager: str) -> PackageEcosystem:
    if manager.startswith(("npm", "yarn")):
        return PackageEcosystem.NPM
    if manager.startswith("cargo"):
        return PackageEcosystem.CARGO
    return PackageEcosystem.PYPI


def classify_package_source(spec: str) -> PackageSource:
    lowered = spec.casefold().strip()
    if _shell_evaluated(spec):
        return PackageSource.SHELL
    target = lowered.split(" @ ", 1)[-1]
    if target.startswith(("git+", "git@", "github:", "gitlab:")) or target.endswith(".git"):
        return PackageSource.VCS
    if "://" in target:
        return PackageSource.DIRECT_URL
    if target.startswith((".", "/", "file:")):
        return PackageSource.LOCAL
    return PackageSource.REGISTRY


def _direct_name(spec: str) -> str | None:
    if " @ " in spec:
        candidate = spec.split(" @ ", 1)[0].strip()
        return candidate or None
    egg = re.search(r"[#&]egg=([A-Za-z0-9_.-]+)", spec)
    return egg.group(1) if egg else None


def parse_npm_coordinate(spec: str) -> tuple[str | None, str | None]:
    if spec.startswith("@"):  # Scoped package: @scope/name or @scope/name@version.
        separator = spec.rfind("@")
        if separator > spec.find("/"):
            return spec[:separator].casefold(), spec[separator + 1 :] or None
        return spec.casefold(), None
    if "@" in spec:
        name, version = spec.rsplit("@", 1)
        return name.casefold() or None, version or None
    return (spec.casefold(), None) if re.fullmatch(r"[A-Za-z0-9_.-]+", spec) else (None, None)


def normalize_package_name(ecosystem: PackageEcosystem, name: str | None) -> str | None:
    if not name or name == "None":
        return None
    if ecosystem is PackageEcosystem.PYPI:
        return re.sub(r"[-_.]+", "-", name).casefold()
    return name.casefold()


def _shell_evaluated(value: str) -> bool:
    return any(marker in value for marker in ("$", "`"))


def _has_custom_registry_option(tokens: list[str]) -> bool:
    return any(
        item in _CUSTOM_REGISTRY_OPTIONS
        or any(item.startswith(f"{option}=") for option in _CUSTOM_REGISTRY_OPTIONS)
        for item in tokens
    )


def _option_value(tokens: list[str], option: str) -> str | None:
    try:
        index = tokens.index(option)
    except ValueError:
        return None
    return tokens[index + 1] if index + 1 < len(tokens) else None


def _ambiguous(ecosystem: PackageEcosystem, intent: PackageIntent) -> PackageReference:
    return PackageReference(ecosystem, None, None, PackageSource.AMBIGUOUS, intent)


def _with_source(reference: PackageReference, source: PackageSource) -> PackageReference:
    return PackageReference(
        reference.ecosystem,
        reference.name,
        reference.version,
        source,
        reference.intent,
        reference.lock_pinned,
    )


def unique_package_references(
    references: list[PackageReference],
) -> tuple[PackageReference, ...]:
    return tuple(dict.fromkeys(references))
