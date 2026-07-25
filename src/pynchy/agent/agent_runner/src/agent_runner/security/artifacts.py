"""Normalize tool calls into security artifacts and apply hard local rules.

Agent cores expose different tool names and input shapes. Security policy must
reason about the operation, not an SDK spelling. This module stays dependency
free because it runs inside every agent container and CLI hook subprocess.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from agent_runner.security.package_manifests import (
    is_package_manifest,
    parse_manifest_write,
)
from agent_runner.security.packages import (
    PackageReference,
    parse_package_commands,
)


class ArtifactKind(StrEnum):
    """Semantic values extracted from a tool request."""

    COMMAND = "command"
    PATH_READ = "path_read"
    PATH_WRITE = "path_write"
    CONTENT = "content"
    URL = "url"
    PACKAGE = "package"


@dataclass(frozen=True)
class SecurityArtifact:
    """One typed value at the agent-tool security boundary."""

    kind: ArtifactKind
    value: str


@dataclass(frozen=True)
class RuleFinding:
    """A deterministic rule match that never contains matched content."""

    rule_id: str
    reason: str


@dataclass(frozen=True)
class NormalizedToolRequest:
    """Core-independent security view of one proposed tool call."""

    tool_name: str
    artifacts: tuple[SecurityArtifact, ...]
    packages: tuple[PackageReference, ...] = ()

    @property
    def accesses_files(self) -> bool:
        """Return whether this operation can observe workspace file data."""
        return any(
            artifact.kind in {ArtifactKind.COMMAND, ArtifactKind.PATH_READ, ArtifactKind.PATH_WRITE}
            for artifact in self.artifacts
        )


_COMMAND_TOOL_NAMES = frozenset({"bash", "shell", "exec", "execute", "local_shell"})
_READ_TOOL_NAMES = frozenset({"read", "grep", "glob", "search", "notebookread"})
_WRITE_TOOL_NAMES = frozenset(
    {"write", "edit", "multiedit", "notebookedit", "applypatch", "apply_patch", "patch"}
)
_PATH_KEYS = ("file_path", "path", "notebook_path")
_CONTENT_KEYS = ("content", "new_string", "new_text", "diff", "patch", "operation")
_URL_KEYS = ("url", "uri", "href")

# NOTE: Update docs/architecture/security.md section 5b when these rule IDs or
# unconditional behaviors change.
_REMOTE_TO_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^\n|;]*(?:\||\|&)\s*(?:sudo\s+)?(?:ba|z|k)?sh\b",
    re.IGNORECASE,
)
_REVERSE_SHELL = re.compile(
    r"(?:/dev/(?:tcp|udp)/|\bnc(?:at)?\b[^\n]*(?:\s-e\s|--exec)|"
    r"\b(?:ba|z|k)?sh\s+-i\b[^\n]*(?:>&|\|)|socket\.(?:create_connection|connect)\()",
    re.IGNORECASE,
)
_DESTRUCTIVE_COMMAND = re.compile(
    r"(?:\brm\s+(?=[^\n;&|]*-[^\n;&|]*r)(?=[^\n;&|]*-[^\n;&|]*f)[^\n;&|]*"
    r"(?:\s/\s*(?:$|[;&|])|\s/(?:etc|usr|var|home|root)(?:/|\s|$))|"
    r"\b(?:mkfs(?:\.[a-z0-9]+)?|wipefs)\b|"
    r"\bdd\b[^\n;&|]*\bof=/dev/(?:sd|nvme|vd)|"
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:)",
    re.IGNORECASE,
)
_PERSISTENCE_PATH_PARTS = (
    "/.config/autostart/",
    "/.config/systemd/user/",
    "/.local/share/systemd/user/",
    "/.ssh/authorized_keys",
    "/etc/cron",
    "/etc/systemd/system/",
    "/library/launchagents/",
    "/library/launchdaemons/",
)
_PERSISTENCE_FILENAMES = frozenset({".bashrc", ".zshrc", ".profile", ".bash_profile"})
_CREDENTIAL_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_rsa",
        "id_ed25519",
        "private_key",
    }
)


def _tool_key(tool_name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", tool_name.casefold())


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if isinstance(item, str)]
    return [str(value)]


def _append_unique(
    artifacts: list[SecurityArtifact], kind: ArtifactKind, values: list[str]
) -> None:
    existing = {(artifact.kind, artifact.value) for artifact in artifacts}
    for value in values:
        stripped = value.strip()
        if stripped and (kind, stripped) not in existing:
            artifacts.append(SecurityArtifact(kind=kind, value=stripped))
            existing.add((kind, stripped))


def _command_values(tool_key: str, tool_input: dict[str, Any]) -> list[str]:
    if tool_key not in _COMMAND_TOOL_NAMES:
        return []
    values: list[str] = []
    for key in ("command", "cmd", "commands", "script"):
        values.extend(_strings(tool_input.get(key)))
    action = tool_input.get("action")
    if isinstance(action, dict):
        for key in ("command", "commands"):
            values.extend(_strings(action.get(key)))
    return values


def _content_values(tool_key: str, tool_input: dict[str, Any]) -> list[str]:
    values = [value for key in _CONTENT_KEYS for value in _strings(tool_input.get(key))]
    if tool_key in _WRITE_TOOL_NAMES:
        # CLI hook transports can put a free-form patch in ``input`` or
        # ``command``. Treat that value as patch content: parsing it as shell
        # code can turn ordinary prose such as "credentials" into CRED001.
        for key in ("input", "command"):
            values.extend(_strings(tool_input.get(key)))
    return values


def _path_kind(tool_key: str, tool_input: dict[str, Any]) -> ArtifactKind:
    if tool_key in _WRITE_TOOL_NAMES or any(key in tool_input for key in _CONTENT_KEYS):
        return ArtifactKind.PATH_WRITE
    return ArtifactKind.PATH_READ


def _patch_paths(contents: list[str]) -> list[str]:
    paths: list[str] = []
    pattern = re.compile(r"^(?:\*\*\* (?:Add|Update) File:|\+\+\+ b/)([^\n]+)$", re.MULTILINE)
    for content in contents:
        paths.extend(match.group(1).strip() for match in pattern.finditer(content))
    return paths


def normalize_tool_request(tool_name: str, tool_input: dict[str, Any]) -> NormalizedToolRequest:
    """Parse SDK-specific tool input into a stable semantic request."""
    tool_key = _tool_key(tool_name)
    artifacts: list[SecurityArtifact] = []

    commands = _command_values(tool_key, tool_input)
    if commands or tool_key in _COMMAND_TOOL_NAMES:
        _append_unique(artifacts, ArtifactKind.COMMAND, commands)

    paths: list[str] = []
    for key in _PATH_KEYS:
        paths.extend(_strings(tool_input.get(key)))
    content_values = _content_values(tool_key, tool_input)
    if tool_key in _WRITE_TOOL_NAMES:
        paths.extend(_patch_paths(content_values))
    if paths or tool_key in _READ_TOOL_NAMES or tool_key in _WRITE_TOOL_NAMES:
        _append_unique(artifacts, _path_kind(tool_key, tool_input), paths)

    _append_unique(artifacts, ArtifactKind.CONTENT, content_values)
    for key in _URL_KEYS:
        _append_unique(artifacts, ArtifactKind.URL, _strings(tool_input.get(key)))

    package_references: list[PackageReference] = []
    for command in commands:
        parsed = parse_package_commands(command)
        package_references.extend(parsed)
        _append_unique(
            artifacts,
            ArtifactKind.PACKAGE,
            [
                f"{reference.ecosystem.value}:{reference.name or '<ambiguous>'}"
                for reference in parsed
            ],
        )
    written_paths = [
        artifact.value for artifact in artifacts if artifact.kind is ArtifactKind.PATH_WRITE
    ]
    contents = tuple(
        artifact.value for artifact in artifacts if artifact.kind is ArtifactKind.CONTENT
    )
    for path in written_paths:
        if is_package_manifest(path):
            package_references.extend(parse_manifest_write(path, contents))

    return NormalizedToolRequest(
        tool_name=tool_name,
        artifacts=tuple(artifacts),
        packages=tuple(dict.fromkeys(package_references)),
    )


def _persistence_path(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").casefold().lstrip("/")
    return PurePosixPath(normalized).name in _PERSISTENCE_FILENAMES or any(
        part in normalized for part in _PERSISTENCE_PATH_PARTS
    )


def _credential_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = {part for part in normalized.split("/") if part}
    return (
        bool(parts & _CREDENTIAL_NAMES)
        or any(part.startswith(".env.") for part in parts)
        or any(marker in normalized for marker in ("/.aws/", "/.gnupg/", "/.kube/"))
    )


def _command_accesses_credential(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(
        _credential_path(token.strip("'\"(){}[],:;"))
        for token in tokens
        if not token.startswith(("http://", "https://"))
    )


def _command_writes_persistence(command: str) -> bool:
    """Detect common shell paths that bypass structured write tools."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens):
        cleaned = token.strip("'\"(){}[],:;")
        if _persistence_path(cleaned) and (
            index > 0
            and (
                tokens[index - 1] in {">", ">>", "1>", "1>>", "2>", "2>>"}
                or any(operator in tokens[index - 1] for operator in (">", ">>"))
                or tokens[0] in {"tee", "cp", "install"}
                or "tee" in tokens[:index]
                or "cp" in tokens[:index]
                or "install" in tokens[:index]
            )
        ):
            return True
        if any(operator in cleaned for operator in (">", ">>")):
            _, _, target = cleaned.rpartition(">")
            if target and _persistence_path(target):
                return True
    return False


def _artifact_findings(artifact: SecurityArtifact) -> tuple[RuleFinding, ...]:
    findings: list[RuleFinding] = []
    if artifact.kind is ArtifactKind.COMMAND:
        checks = (
            (
                _DESTRUCTIVE_COMMAND.search(artifact.value),
                "CMD001",
                "Destructive command targets host or durable system state",
            ),
            (
                _REVERSE_SHELL.search(artifact.value),
                "NET001",
                "Reverse-shell behavior is prohibited",
            ),
            (
                _REMOTE_TO_SHELL.search(artifact.value),
                "NET002",
                "Remote content cannot be piped directly to a shell",
            ),
            (
                _command_accesses_credential(artifact.value),
                "CRED001",
                "Credential-file access establishes secret taint",
            ),
            (
                _command_writes_persistence(artifact.value),
                "PERSIST001",
                "Writing to an autostart or persistence path is prohibited",
            ),
        )
        findings.extend(
            RuleFinding(rule_id, reason) for matched, rule_id, reason in checks if matched
        )
    elif artifact.kind is ArtifactKind.PATH_WRITE and _persistence_path(artifact.value):
        findings.append(
            RuleFinding(
                "PERSIST001",
                "Writing to an autostart or persistence path is prohibited",
            )
        )
    elif artifact.kind is ArtifactKind.PATH_READ and _credential_path(artifact.value):
        findings.append(RuleFinding("CRED001", "Credential-file access establishes secret taint"))
    return tuple(findings)


def _package_findings(package: PackageReference) -> tuple[RuleFinding, ...]:
    findings: list[RuleFinding] = []
    if package.source.value == "shell":
        findings.append(
            RuleFinding("PKG002", "Shell-evaluated package specifications are prohibited")
        )
    elif package.source.value in {"direct_url", "vcs", "local", "custom_registry"}:
        findings.append(RuleFinding("PKG001", "Direct package sources require human approval"))
    elif package.name is None or package.source.value == "ambiguous":
        findings.append(RuleFinding("PKG003", "Package name could not be determined unambiguously"))
    if package.intent.value == "executable" and package.version is None:
        findings.append(
            RuleFinding(
                "PKG004",
                "Unpinned executable package installs require human approval",
            )
        )
    return tuple(findings)


def deterministic_findings(request: NormalizedToolRequest) -> tuple[RuleFinding, ...]:
    """Return unconditional local rule matches for a normalized request."""
    all_findings = (
        *(finding for artifact in request.artifacts for finding in _artifact_findings(artifact)),
        *(finding for package in request.packages for finding in _package_findings(package)),
    )
    return tuple({finding.rule_id: finding for finding in all_findings}.values())
