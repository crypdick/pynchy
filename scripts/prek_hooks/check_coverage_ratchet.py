"""Keep the coverage floor from falling behind committed or measured coverage."""

from __future__ import annotations

import argparse
import io
import re
import subprocess  # noqa: S404 - reads fixed local Git refs without a shell.
import sys
import tomllib
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from coverage import Coverage
from coverage.exceptions import CoverageException

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_BASELINE_REFS = ("HEAD", "main", "origin/main")
_RATCHET_LINE = re.compile(
    r"(?ms)(^\[tool\.coverage\.report\]\s*$.*?^fail_under\s*=\s*)"
    r"([0-9]+(?:\.[0-9]+)?)"
)


def read_ratchet(content: str) -> Decimal:
    """Return the configured coverage floor."""
    try:
        value = tomllib.loads(content)["tool"]["coverage"]["report"]["fail_under"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("pyproject.toml must define tool.coverage.report.fail_under") from error
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        raise ValueError("tool.coverage.report.fail_under must be a number from 0 through 100")
    return Decimal(str(value))


def minimum_allowed_ratchet(committed: Iterable[Decimal]) -> Decimal | None:
    """Return the highest committed floor, if any."""
    values = tuple(committed)
    return max(values) if values else None


def committed_ratchets(project_file: Path) -> list[Decimal]:
    """Read coverage floors from the refs that can constrain this checkout."""
    relative_path = project_file.resolve().relative_to(Path.cwd().resolve()).as_posix()
    ratchets: list[Decimal] = []
    for ref in _BASELINE_REFS:
        result = subprocess.run(  # noqa: S603 - fixed Git read with no shell.
            ["git", "show", f"{ref}:{relative_path}"],  # noqa: S607 - Git is required here.
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            ratchets.append(read_ratchet(result.stdout))
    return ratchets


def measured_ratchet() -> Decimal:
    """Return a coverage floor that the current result can satisfy."""
    coverage = Coverage()
    coverage.load()
    total = coverage.report(file=io.StringIO())
    precision = coverage.config.precision
    increment = Decimal(1).scaleb(-precision)
    return Decimal(str(total)).quantize(increment, rounding=ROUND_FLOOR)


def raise_ratchet(project_file: Path, measured: Decimal) -> bool:
    """Raise the configured floor to *measured* without rewriting unrelated TOML."""
    content = project_file.read_text(encoding="utf-8")
    current = read_ratchet(content)
    if measured <= current:
        return False
    replacement = format(measured, "f")
    updated, count = _RATCHET_LINE.subn(rf"\g<1>{replacement}", content, count=1)
    if count != 1:
        raise ValueError("could not locate tool.coverage.report.fail_under in pyproject.toml")
    project_file.write_text(updated, encoding="utf-8")
    return True


def check_ratchet(project_file: Path, *, update: bool) -> int:
    """Enforce committed floors and optionally raise the current floor."""
    content = project_file.read_text(encoding="utf-8")
    current = read_ratchet(content)
    required = minimum_allowed_ratchet(committed_ratchets(project_file))
    if required is not None and current < required:
        print(
            f"Coverage ratchet cannot fall from {required}% to {current}%.",
            file=sys.stderr,
        )
        return 1
    if update:
        measured = measured_ratchet()
        if raise_ratchet(project_file, measured):
            print(f"Raised coverage ratchet from {current}% to {measured}%.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Check the committed floor and optionally raise it from coverage data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="raise the floor from .coverage")
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    try:
        return check_ratchet(args.project_file, update=args.update)
    except (CoverageException, OSError, ValueError) as error:
        print(f"Coverage ratchet check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
