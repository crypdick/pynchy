"""Repository-wide hygiene checks for portable public source."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - test invokes fixed git argv with no shell.
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GIT = shutil.which("git") or "/usr/bin/git"
_FORBIDDEN_MARKERS = (
    "ri" + "cardo",
    "/" + "src" + "/" + "PERSONAL",
    "mac" + "-mini",
    "d" + "cloud",
    "tank" + "-20",
)


def _non_ignored_repo_paths() -> list[Path]:
    result = subprocess.run(  # noqa: S603 - fixed no-shell git argv.
        [_GIT, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    raw_paths = [path for path in result.stdout.split(b"\0") if path]
    return [_REPO_ROOT / raw_path.decode("utf-8") for raw_path in raw_paths]


def _matching_lines(path: Path, marker: str) -> list[tuple[int, str]]:
    if not path.is_file():
        return []
    marker_bytes = marker.encode("utf-8").lower()
    content = path.read_bytes()
    if marker_bytes not in content.lower():
        return []
    return [
        (line_number, line.decode("utf-8", errors="replace").strip())
        for line_number, line in enumerate(content.splitlines(), start=1)
        if marker_bytes in line.lower()
    ]


def test_repo_does_not_contain_maintainer_local_markers() -> None:
    findings: list[str] = []

    for path in _non_ignored_repo_paths():
        relative_path = path.relative_to(_REPO_ROOT)
        # Copyright attribution is public metadata, not a maintainer-local leak.
        if relative_path == Path("LICENSE"):
            continue
        relative_text = relative_path.as_posix().casefold()
        for marker in _FORBIDDEN_MARKERS:
            if marker.casefold() in relative_text:
                findings.append(f"{relative_path}: path contains {marker!r}")
            findings.extend(
                f"{relative_path}:{line_number}: contains {marker!r}: {line_text}"
                for line_number, line_text in _matching_lines(path, marker)
            )

    assert not findings, (
        "Do not let personal details of the code maintainer bleed into prod code. "
        "Use neutral examples, placeholders, config/env discovery, or gitignored "
        "local files instead.\n\n" + "\n".join(findings)
    )
