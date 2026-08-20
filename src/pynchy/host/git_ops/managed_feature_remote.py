"""Remote ref helpers for managed feature validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pynchy.host.git_ops.utils import run_git

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class _GitTransport(Protocol):
    """Host-owned Git metadata used to query one remote repository."""

    @property
    def root(self) -> Path: ...

    @property
    def args(self) -> tuple[str, ...]: ...

    def environment(self) -> Mapping[str, str]: ...


def remote_default_branch(
    transport: _GitTransport,
    remote_url: str,
    *,
    git_runner: object = run_git,
) -> str | None:
    """Return remote symbolic HEAD branch without local Git configuration."""
    result = git_runner(  # type: ignore[operator]
        *transport.args,
        "ls-remote",
        "--symref",
        remote_url,
        "HEAD",
        cwd=transport.root,
        env=transport.environment(),
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        marker = "ref: refs/heads/"
        if line.startswith(marker) and line.endswith("\tHEAD"):
            branch = line[len(marker) : -len("\tHEAD")]
            return branch or None
    return None


def remote_ref_sha(output: str, remote_ref: str) -> str | None:
    """Return one remote SHA, empty when a ref does not exist."""
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        return None
    sha, separator, ref = lines[0].partition("\t")
    if separator != "\t" or ref != remote_ref or len(sha) not in {40, 64}:
        return None
    if any(character not in "0123456789abcdef" for character in sha):
        return None
    return sha
