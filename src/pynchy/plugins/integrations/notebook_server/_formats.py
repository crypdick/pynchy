""".qmd parsing/serialization and notebook I/O.

All functions are pure (no module-level state) and depend only on ``nbformat``.
Functions that need a notebook directory accept it as an explicit parameter.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

if TYPE_CHECKING:
    from collections.abc import Callable

# nbformat publishes no type information. Keep that boundary at its constructors
# and I/O functions rather than letting untyped calls flow through this module.
_new_notebook = cast("Callable[[], object]", new_notebook)
_new_code_cell = cast("Callable[..., object]", new_code_cell)
_new_markdown_cell = cast("Callable[..., object]", new_markdown_cell)
_read_notebook = cast("Callable[..., object]", nbformat.read)
_write_notebook = cast("Callable[..., None]", nbformat.write)


def generate_name() -> str:
    """Generate a notebook name: YYYY-MM-DD-adjective-animal."""
    from ubuntu_namer import (  # noqa: PLC0415 - optional plugin dependency.
        generate_name,
    )

    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    slug = generate_name(style="kebab")
    return f"{today}-{slug}"


def notebook_path(name: str, notebook_dir: Path) -> Path:
    """Resolve notebook name to full path, adding .qmd if no extension."""
    if not name.endswith((".ipynb", ".qmd")):
        name = f"{name}.qmd"
    return notebook_dir / name


# ---------------------------------------------------------------------------
# .qmd parsing / serialization
# ---------------------------------------------------------------------------


def _empty_notebook() -> object:
    nb = _new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    return nb


def _flush_markdown_cell(nb: object, lines: list[str]) -> list[str]:
    if not lines:
        return []
    content = "\n".join(lines).strip()
    if content:
        nb.cells.append(_new_markdown_cell(source=content))
    return []


def _append_code_cell(nb: object, lines: list[str]) -> list[str]:
    nb.cells.append(_new_code_cell(source="\n".join(lines)))
    return []


def _starts_python_fence(line: str) -> bool:
    return line.strip().startswith("```{python}")


def _ends_code_fence(line: str) -> bool:
    return line.strip() == "```"


def parse_qmd(text: str) -> object:
    """Parse a .qmd file into a notebook node.

    Code fences with ``{python}`` become code cells; everything else becomes
    markdown cells.
    """
    nb = _empty_notebook()

    lines = text.split("\n")
    current_md: list[str] = []
    current_code: list[str] = []
    in_code_block = False

    for line in lines:
        if not in_code_block and _starts_python_fence(line):
            current_md = _flush_markdown_cell(nb, current_md)
            in_code_block = True
            continue
        if in_code_block and _ends_code_fence(line):
            current_code = _append_code_cell(nb, current_code)
            in_code_block = False
            continue
        if in_code_block:
            current_code.append(line)
        else:
            current_md.append(line)

    _flush_markdown_cell(nb, current_md)
    return nb


def serialize_qmd(nb: object) -> str:
    """Serialize a notebook node to .qmd format."""
    parts: list[str] = []
    for cell in nb.cells:
        if cell.cell_type == "code":
            parts.append(f"```{{python}}\n{cell.source}\n```")
        else:
            parts.append(cell.source)
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Notebook I/O
# ---------------------------------------------------------------------------


def load_notebook(path: Path) -> object:
    """Load a notebook from disk (.ipynb or .qmd)."""
    if path.suffix == ".qmd":
        return parse_qmd(path.read_text(encoding="utf-8"))
    return _read_notebook(str(path), as_version=4)


def save_notebook(nb: object, path: Path) -> None:
    """Save a notebook to disk (.ipynb or .qmd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".qmd":
        path.write_text(serialize_qmd(nb), encoding="utf-8")
    else:
        _write_notebook(nb, str(path))
