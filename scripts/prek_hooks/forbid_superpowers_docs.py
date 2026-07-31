"""Reject commits that add, edit, rename, or delete docs/superpowers content."""

import subprocess  # noqa: S404 - required to inspect the local Git index.
import sys
from pathlib import Path


def blocked_paths(filenames: list[str]) -> list[str]:
    """Return staged paths under the retired plans directory."""
    return [
        filename for filename in filenames if Path(filename).parts[:2] == ("docs", "superpowers")
    ]


def staged_paths() -> list[str]:
    """Return paths in the Git index, rather than prek's optional all-files input."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],  # noqa: S607 - Git is required here.
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def main() -> int:
    """Explain where plans belong when a blocked path is staged."""
    paths = blocked_paths(staged_paths())
    if not paths:
        return 0

    print("Do not commit items under docs/superpowers. Register plans on the Linear board instead.")
    print("Blocked paths:")
    print(*paths, sep="\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
