#!/usr/bin/env -S uv run python
"""Install user-local CLI dependencies for isolated Pynchy runtimes."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess  # noqa: S404 - fixed package-manager commands install pinned tools.
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

_TEMPORAL_VERSION = "1.8.0"
_NEW_FEATURE_VERSION = "1.1.14"
_CODEX_VERSION = "0.144.1"
_TEMPORAL_BASE_URL = "https://github.com/temporalio/cli/releases/download"
_TEMPORAL_VERSION_PATTERN = re.compile(r"\btemporal version v?([^\s]+)")
_NEW_FEATURE_VERSION_PATTERN = re.compile(r"\bnew-feature\s+v?([^\s]+)")
_TEMPORAL_DIGESTS = {
    (
        "darwin",
        "amd64",
    ): "7ea6edf15329e8169233d3e38a0c1f6464cf84ee25140c16ff059ea4f802762e",  # pragma: allowlist secret  # noqa: E501
    (
        "darwin",
        "arm64",
    ): "46b4ac2b603e2b68d684da728bccd938a69acfad9c5e1a469d28d00a64e8bc9c",  # pragma: allowlist secret  # noqa: E501
    (
        "linux",
        "amd64",
    ): "896c6132d6d969f84c3f2382a31abd9a67a06ed3008c1a37c3573fe81d730e4a",  # pragma: allowlist secret  # noqa: E501
    (
        "linux",
        "arm64",
    ): "52d2d3e4f35c4ad2d45d0677eae1e1e3c7ba3c7f40a6a42d9a7f34e541c3dd57",  # pragma: allowlist secret  # noqa: E501
}


class DependencyError(RuntimeError):
    """A sandbox dependency cannot be installed or verified safely."""


def _line(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architectures = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    architecture = architectures.get(machine, machine)
    key = (system, architecture)
    if key not in _TEMPORAL_DIGESTS:
        raise DependencyError(f"Unsupported Temporal CLI platform: {system}/{architecture}")
    return key


def _temporal_archive_name(system: str, architecture: str) -> str:
    return f"temporal_cli_{_TEMPORAL_VERSION}_{system}_{architecture}.tar.gz"


def _download(url: str) -> bytes:
    if not url.startswith("https://github.com/temporalio/cli/releases/download/"):
        raise DependencyError(f"Refusing untrusted download URL: {url}")
    request = urllib.request.Request(  # noqa: S310 - URL prefix is allowlisted above.
        url, headers={"User-Agent": "pynchy-dependency-installer"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - URL prefix is allowlisted above.
            return bytes(response.read())
    except OSError as exc:
        raise DependencyError(f"Failed to download Temporal CLI: {exc}") from exc


def _verify_digest(payload: bytes, expected: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise DependencyError(
            f"Temporal CLI checksum mismatch: expected {expected}, received {actual}"
        )


def _read_temporal_member(archive: tarfile.TarFile) -> bytes:
    candidates = [
        member
        for member in archive.getmembers()
        if member.isfile() and Path(member.name).name == "temporal"
    ]
    if len(candidates) != 1:
        raise DependencyError("Temporal archive must contain exactly one temporal binary")
    extracted = archive.extractfile(candidates[0])
    if extracted is None:
        raise DependencyError("Temporal binary could not be read from the archive")
    return extracted.read()


def _temporal_binary(payload: bytes) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            return _read_temporal_member(archive)
    except tarfile.TarError as exc:
        raise DependencyError(f"Invalid Temporal CLI archive: {exc}") from exc


def _install_temporal(bin_dir: Path) -> Path:
    system, architecture = _platform_key()
    archive_name = _temporal_archive_name(system, architecture)
    url = f"{_TEMPORAL_BASE_URL}/v{_TEMPORAL_VERSION}/{archive_name}"
    _line(f"Downloading Temporal CLI v{_TEMPORAL_VERSION} for {system}/{architecture}...")
    payload = _download(url)
    _verify_digest(payload, _TEMPORAL_DIGESTS[system, architecture])
    binary = _temporal_binary(payload)

    bin_dir.mkdir(parents=True, exist_ok=True)
    destination = bin_dir / "temporal"
    with tempfile.NamedTemporaryFile(dir=bin_dir, prefix=".temporal-", delete=False) as temp:
        temp.write(binary)
        temp_path = Path(temp.name)
    temp_path.chmod(0o755)
    temp_path.replace(destination)
    _line(f"Installed Temporal CLI at {destination}")
    return destination


def _selected_command(name: str, bin_dir: Path) -> str | None:
    selected = bin_dir / name
    return str(selected) if selected.is_file() else None


def _resolved_command(name: str, bin_dir: Path) -> str | None:
    return _selected_command(name, bin_dir) or shutil.which(name)


def _run_checked(command: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(  # noqa: S603 - argv is a fixed pinned install command.
        command,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise DependencyError(f"Dependency install command failed: {' '.join(command)}")


def _install_new_feature(uv: str, bin_dir: Path) -> None:
    env = dict(os.environ)
    env["UV_TOOL_BIN_DIR"] = str(bin_dir)
    _run_checked(
        [uv, "tool", "install", "--force", f"new-feature=={_NEW_FEATURE_VERSION}"], env=env
    )


def _install_codex(npm: str, bin_dir: Path) -> None:
    if bin_dir.name != "bin":
        raise DependencyError("Codex user-local installation requires a bin directory")
    prefix = bin_dir.parent
    _run_checked(
        [
            npm,
            "install",
            "--global",
            "--prefix",
            str(prefix),
            f"@openai/codex@{_CODEX_VERSION}",
        ]
    )


def _docker_ready(docker: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed read-only Docker readiness check.
        [docker, "info"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _temporal_version(temporal: Path) -> str | None:
    """Return the installed Temporal CLI version without trusting PATH order."""
    try:
        result = subprocess.run(  # noqa: S603 - Temporal path is inside the selected runtime bin directory.
            [str(temporal), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _TEMPORAL_VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


def _new_feature_version(new_feature: str) -> str | None:
    """Return the installed new-feature version for the selected command."""
    try:
        result = subprocess.run(  # noqa: S603 - selected tool command is locally resolved.
            [new_feature, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _NEW_FEATURE_VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


def _ensure_pinned_temporal(bin_dir: Path, *, check_only: bool) -> Path:
    """Install or verify the exact Temporal version used by the runtime profile."""
    temporal = bin_dir / "temporal"
    if temporal.is_file() and _temporal_version(temporal) == _TEMPORAL_VERSION:
        return temporal
    if check_only:
        if temporal.exists():
            actual = _temporal_version(temporal) or "unreadable"
            raise DependencyError(
                f"Temporal CLI at {temporal} must be v{_TEMPORAL_VERSION}; found {actual}"
            )
        raise DependencyError(f"Pinned Temporal CLI v{_TEMPORAL_VERSION} is missing from {bin_dir}")
    return _install_temporal(bin_dir)


def _ensure_pinned_new_feature(uv: str, bin_dir: Path, *, check_only: bool) -> str:
    """Install or verify the exact new-feature version required by this configuration."""
    new_feature = _selected_command("new-feature", bin_dir)
    actual = _new_feature_version(new_feature) if new_feature is not None else None
    if new_feature is not None and actual == _NEW_FEATURE_VERSION:
        return new_feature
    if check_only:
        if new_feature is None:
            raise DependencyError(
                f"Pinned new-feature v{_NEW_FEATURE_VERSION} is missing from {bin_dir}"
            )
        message = (
            f"new-feature at {new_feature} must be v{_NEW_FEATURE_VERSION}; "
            f"found {actual or 'unreadable'}"
        )
        raise DependencyError(message)

    _install_new_feature(uv, bin_dir)
    new_feature = _selected_command("new-feature", bin_dir)
    if new_feature is None:
        raise DependencyError("new-feature installation completed without an executable")
    actual = _new_feature_version(new_feature)
    if actual != _NEW_FEATURE_VERSION:
        raise DependencyError(
            "new-feature installation must provide "
            f"v{_NEW_FEATURE_VERSION}; found {actual or 'unreadable'}"
        )
    return new_feature


def _ensure_runtime_dependencies(*, bin_dir: Path, check_only: bool) -> None:
    """Install or verify the Docker and Temporal dependencies shared by CI and development."""
    docker = shutil.which("docker")
    if docker is None:
        raise DependencyError("Docker is required; install Docker Desktop or Docker Engine")
    if not _docker_ready(docker):
        raise DependencyError("Docker is installed but its daemon is not running")
    _line("Docker: ready")

    temporal = _ensure_pinned_temporal(bin_dir, check_only=check_only)
    _line(f"Temporal CLI: {temporal}")


def _ensure_dependencies(*, bin_dir: Path, check_only: bool) -> None:
    """Install the full new-feature developer toolchain."""
    uv = shutil.which("uv")
    if uv is None:
        raise DependencyError("uv is required to run this installer; install it from astral.sh/uv")

    _ensure_runtime_dependencies(bin_dir=bin_dir, check_only=check_only)

    new_feature = _ensure_pinned_new_feature(uv, bin_dir, check_only=check_only)
    _line(f"new-feature: {new_feature}")

    codex = _resolved_command("codex", bin_dir)
    if codex is None:
        if check_only:
            raise DependencyError("Codex CLI is missing")
        npm = shutil.which("npm")
        if npm is None:
            raise DependencyError(
                "Codex CLI is missing and npm is unavailable; install Node.js or Codex manually"
            )
        _install_codex(npm, bin_dir)
        codex = _resolved_command("codex", bin_dir)
    if codex is None:
        raise DependencyError("Codex installation completed without an executable")
    _line(f"Codex CLI: {codex}")

    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        _line(f"Add {bin_dir} to PATH before running new-feature")


def _ensure_selected_dependencies(*, bin_dir: Path, check_only: bool, runtime_only: bool) -> None:
    """Install or verify the CLI dependency profile selected at the command line."""
    resolved_bin_dir = bin_dir.expanduser().resolve()
    if runtime_only:
        _ensure_runtime_dependencies(bin_dir=resolved_bin_dir, check_only=check_only)
        if str(resolved_bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            _line(f"Add {resolved_bin_dir} to PATH before running the deterministic runtime")
        return
    _ensure_dependencies(bin_dir=resolved_bin_dir, check_only=check_only)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="user-local executable directory (default: ~/.local/bin)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the selected dependency profile without installing missing CLI tools",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="verify Docker and install or verify only the pinned Temporal CLI",
    )
    args = parser.parse_args()
    try:
        _ensure_selected_dependencies(
            bin_dir=args.bin_dir,
            check_only=args.check,
            runtime_only=args.runtime_only,
        )
    except DependencyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
