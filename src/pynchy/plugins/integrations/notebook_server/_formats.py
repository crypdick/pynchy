""".qmd parsing/serialization and notebook I/O.

All functions are pure (no module-level state) and depend only on ``nbformat``.
Functions that need a notebook directory accept it as an explicit parameter.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import cast

import nbformat
from nbformat.notebooknode import NotebookNode
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook


def generate_name() -> str:
    """Generate a notebook name: YYYY-MM-DD-adjective-animal."""
    from ubuntu_namer import generate

    today = datetime.date.today().isoformat()
    slug = generate()  # e.g. "ailing-amoeba"
    return f"{today}-{slug}"


def notebook_path(name: str, notebook_dir: Path) -> Path:
    """Resolve notebook name to full path, adding .qmd if no extension."""
    if not name.endswith((".ipynb", ".qmd")):
        name = f"{name}.qmd"
    return notebook_dir / name


# ---------------------------------------------------------------------------
# .qmd parsing / serialization
# ---------------------------------------------------------------------------


def _empty_notebook() -> NotebookNode:
    nb: NotebookNode = new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    return nb


def _flush_markdown_cell(nb: NotebookNode, lines: list[str]) -> list[str]:
    if not lines:
        return []
    content = "\n".join(lines).strip()
    if content:
        nb.cells.append(new_markdown_cell(source=content))
    return []


def _append_code_cell(nb: NotebookNode, lines: list[str]) -> list[str]:
    nb.cells.append(new_code_cell(source="\n".join(lines)))
    return []


def _starts_python_fence(line: str) -> bool:
    return line.strip().startswith("```{python}")


def _ends_code_fence(line: str) -> bool:
    return line.strip() == "```"


def parse_qmd(text: str) -> NotebookNode:
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


def serialize_qmd(nb: NotebookNode) -> str:
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


def load_notebook(path: Path) -> NotebookNode:
    """Load a notebook from disk (.ipynb or .qmd)."""
    if path.suffix == ".qmd":
        return parse_qmd(path.read_text())
    return cast("NotebookNode", nbformat.read(str(path), as_version=4))


def save_notebook(nb: NotebookNode, path: Path) -> None:
    """Save a notebook to disk (.ipynb or .qmd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".qmd":
        path.write_text(serialize_qmd(nb))
    else:
        nbformat.write(nb, str(path))
