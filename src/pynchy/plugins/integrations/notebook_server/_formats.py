""".qmd parsing/serialization and notebook I/O.

All functions are pure (no module-level state) and depend only on ``nbformat``.
Functions that need a notebook directory accept it as an explicit parameter.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import nbformat
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


def parse_qmd(text: str) -> nbformat.NotebookNode:
    """Parse a .qmd file into a notebook node.

    Code fences with ``{python}`` become code cells; everything else becomes
    markdown cells.
    """
    nb = new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }

    lines = text.split("\n")
    current_md: list[str] = []
    current_code: list[str] = []
    in_code = False

    for line in lines:
        stripped = line.strip()
        if not in_code and stripped.startswith("```{python}"):
            # Flush accumulated markdown
            if current_md:
                content = "\n".join(current_md).strip()
                if content:
                    nb.cells.append(new_markdown_cell(source=content))
                current_md = []
            in_code = True
            continue
        if in_code and stripped == "```":
            # End of code fence
            nb.cells.append(new_code_cell(source="\n".join(current_code)))
            current_code = []
            in_code = False
            continue
        if in_code:
            current_code.append(line)
        else:
            current_md.append(line)

    # Flush remaining markdown
    if current_md:
        content = "\n".join(current_md).strip()
        if content:
            nb.cells.append(new_markdown_cell(source=content))

    return nb


def serialize_qmd(nb: nbformat.NotebookNode) -> str:
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


def load_notebook(path: Path) -> nbformat.NotebookNode:
    """Load a notebook from disk (.ipynb or .qmd)."""
    if path.suffix == ".qmd":
        return parse_qmd(path.read_text())
    return nbformat.read(str(path), as_version=4)


def save_notebook(nb: nbformat.NotebookNode, path: Path) -> None:
    """Save a notebook to disk (.ipynb or .qmd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".qmd":
        path.write_text(serialize_qmd(nb))
    else:
        nbformat.write(nb, str(path))
