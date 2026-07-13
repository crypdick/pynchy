#!/usr/bin/env python3
"""Compatibility entry point for a new-feature deterministic runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.runtime_harness import main
else:
    from runtime_harness import main  # type: ignore[import-not-found, no-redef]


if __name__ == "__main__":
    main()
