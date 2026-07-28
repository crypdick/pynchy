"""Regression coverage for the notebook image's isolated package import."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - test runs the current interpreter with a fixed script.
import sys
from pathlib import Path

_BLOCK_PYNCHY_IMPORTS = """
import importlib.abc
import sys

class BlockPynchy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pynchy' or fullname.startswith('pynchy.'):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockPynchy())
import notebook_server
"""


def test_notebook_package_imports_without_the_host_package(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src/pynchy/plugins/integrations/notebook_server"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(source, tmp_path / "notebook_server", ignore=ignore)

    result = subprocess.run(  # noqa: S603 - executable and arguments are fixed test inputs.
        [sys.executable, "-c", _BLOCK_PYNCHY_IMPORTS],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
