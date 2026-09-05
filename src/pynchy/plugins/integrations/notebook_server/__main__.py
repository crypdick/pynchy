"""Notebook execution MCP server — FastMCP + jupyter_client + JupyterLab.

Entry point for ``python -m notebook_server`` (Docker) or
``python -m pynchy.plugins.integrations.notebook_server`` (host).
Heavy dependencies (JupyterLab, IPython kernel, FastMCP) are resolved here
and never imported by the plugin class or the main pynchy process.

Provides MCP tools for agents to create, execute, and manage Jupyter notebooks.
JupyterLab runs as a separate subprocess for human viewing via Tailscale.

Usage (Docker)::

    python -m notebook_server --workspace research --workspace-dir /workspace --port 8460

Usage (host)::

    python -m pynchy.plugins.integrations.notebook_server --workspace research --port 8460
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess  # noqa: S404 - launches JupyterLab with fixed argv and sys.executable.
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastmcp import FastMCP
from jupyter_client import KernelManager
from nbformat import from_dict
from nbformat.v4 import new_code_cell, new_markdown_cell

from ._execution import (
    KERNEL_STARTUP_CODE,
    KernelSession,
    execute_code,
)
from ._formats import (
    generate_name,
    load_notebook,
    notebook_path,
    save_notebook,
)
from ._output import (
    outputs_for_agent,
    save_cell_images,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# nbformat publishes no type information. Restrict its dynamic values to these
# constructors and conversion boundary functions.
_from_dict = cast("Callable[[dict[str, object]], object]", from_dict)
_new_code_cell = cast("Callable[..., object]", new_code_cell)
_new_markdown_cell = cast("Callable[..., object]", new_markdown_cell)

CONTAINER_BIND_HOST = "0.0.0.0"  # noqa: S104 - notebook MCP/Jupyter run in Docker and must be reachable from sibling containers and Tailscale.

# ---------------------------------------------------------------------------
# CLI argument parsing (workspace kwargs arrive as --key value pairs)
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Pynchy project root — set by the host environment or inferred from cwd."""
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notebook MCP server")
    parser.add_argument(
        "--workspace",
        default="default",
        help="Workspace name (used for logging and kernel cwd)",
    )
    parser.add_argument(
        "--workspace-dir",
        default=None,
        help="Explicit workspace directory path. When provided (e.g., in Docker "
        "via --workspace-dir /workspace), used directly instead of constructing "
        "from project root + workspace name.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8460,
        help="FastMCP server port (default: 8460)",
    )
    parser.add_argument(
        "--lab_port",
        type=int,
        default=8888,
        help="JupyterLab port (default: 8888)",
    )
    return parser.parse_args()


ARGS = _parse_args()
# In Docker: --workspace-dir /workspace (the bind mount).
# On host: derive from project root + workspace name.
if ARGS.workspace_dir:
    WORKSPACE_DIR = Path(ARGS.workspace_dir).resolve()
else:
    _ROOT = _project_root()
    WORKSPACE_DIR = (_ROOT / "groups" / ARGS.workspace).resolve()
NOTEBOOK_DIR = (WORKSPACE_DIR / "notebooks").resolve()
NOTEBOOK_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("Notebook Server")


# ---------------------------------------------------------------------------
# Kernel session state
# ---------------------------------------------------------------------------

# kernel_id -> KernelSession
_sessions: dict[str, KernelSession] = {}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


# FastMCP's decorator is untyped, so each tool needs a local mypy ignore.


def _stdout_line(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


@mcp.tool()  # type: ignore[untyped-decorator]
async def start_kernel(name: str | None = None) -> dict[str, Any]:
    """Start an IPython kernel, optionally loading an existing notebook.

    If ``name`` refers to an existing notebook on disk, loads it and
    re-executes all code cells to restore kernel state. If omitted, creates
    a notebook with an auto-generated name.

    Args:
        name: Notebook name (without path). If the notebook exists on disk,
              its cells are re-executed to restore state. If omitted, a
              name is auto-generated.

    Returns:
        Session info: kernel_id, notebook name, cell summary (if rehydrating).
    """

    if name is None:
        name = generate_name()

    # Strip extension for internal use
    base_name = name.removesuffix(".ipynb").removesuffix(".qmd")

    km = KernelManager(kernel_name="python3")
    km.start_kernel(cwd=str(WORKSPACE_DIR))
    client = km.client()
    client.start_channels()
    # Wait for kernel to be ready
    try:
        client.wait_for_ready(timeout=30)
    except RuntimeError as e:
        km.shutdown_kernel(now=True)
        return {"error": f"Kernel failed to start: {e}"}

    kernel_id = str(uuid.uuid4())[:8]
    session = KernelSession(kernel_id, km, client, base_name)
    _sessions[kernel_id] = session

    # Silent startup: configure pandas/matplotlib defaults for agent-friendly output.
    # Not saved to the notebook — runs before any user cells.
    await execute_code(session, KERNEL_STARTUP_CODE)

    nb_path = notebook_path(base_name, NOTEBOOK_DIR)
    result: dict[str, Any] = {
        "kernel_id": kernel_id,
        "notebook": nb_path.name,
        "status": "started",
    }

    # Rehydrate if notebook exists on disk
    if nb_path.exists():
        existing_nb = load_notebook(nb_path)
        session.nb = existing_nb
        errors: list[str] = []
        code_count = 0
        for cell in existing_nb.cells:
            if cell.cell_type == "code":
                code_count += 1
                outputs = await execute_code(session, cell.source)
                cell.outputs = [_from_dict(output) for output in outputs]
                errors.extend(
                    f"Cell {code_count}: {out.get('ename')}: {out.get('evalue')}"
                    for out in outputs
                    if out.get("output_type") == "error"
                )

        result["rehydrated"] = True
        result["cells_total"] = len(existing_nb.cells)
        result["code_cells_executed"] = code_count
        if errors:
            result["replay_errors"] = errors
        result["status"] = "rehydrated"
    else:
        result["rehydrated"] = False

    return result


@mcp.tool()  # type: ignore[untyped-decorator]
async def execute_cell(kernel_id: str, code: str) -> dict[str, Any]:
    """Execute Python code in a running kernel.

    Appends the code cell and its outputs to the in-memory notebook, then
    auto-saves to disk.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.
        code: Python code to execute.

    Returns:
        Cell outputs (text, images, errors) in a simplified format.
    """

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    outputs = await execute_code(session, code)

    # Append the code cell before deriving its image filenames from its final position.
    cell = _new_code_cell(source=code)
    session.nb.cells.append(cell)
    cell_number = len(session.nb.cells)

    # Save images to disk (mutates outputs in-place with _image_path)
    save_cell_images(session.name, cell_number, outputs, NOTEBOOK_DIR)
    # nbformat serializers require NotebookNode outputs, not plain dictionaries.
    cell.outputs = [_from_dict(output) for output in outputs]

    # Auto-save
    save_notebook(session.nb, notebook_path(session.name, NOTEBOOK_DIR))

    return {
        "cell_number": cell_number,
        "outputs": outputs_for_agent(outputs),
    }


@mcp.tool()  # type: ignore[untyped-decorator]
async def add_markdown(kernel_id: str, content: str) -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """Add a markdown cell to the notebook.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.
        content: Markdown text for the cell.

    Returns:
        Confirmation with cell number.
    """

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    session.nb.cells.append(_new_markdown_cell(source=content))
    nb_path = notebook_path(session.name, NOTEBOOK_DIR)
    save_notebook(session.nb, nb_path)

    return {
        "cell_number": len(session.nb.cells),
        "notebook": nb_path.name,
    }


@mcp.tool()  # type: ignore[untyped-decorator]
async def save_as(kernel_id: str, name: str) -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """Save the current notebook under a different name.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.
        name: Notebook name to save under (without path).

    Returns:
        Confirmation with the saved filename and cell count.
    """

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    base_name = name.removesuffix(".ipynb").removesuffix(".qmd")
    ext = ".ipynb" if name.endswith(".ipynb") else ".qmd"
    new_path = NOTEBOOK_DIR / f"{base_name}{ext}"

    save_notebook(session.nb, new_path)
    session.name = base_name

    return {
        "notebook": new_path.name,
        "cells": len(session.nb.cells),
    }


@mcp.tool()  # type: ignore[untyped-decorator]
async def read_notebook(name: str) -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """Read an existing notebook without starting a kernel.

    Args:
        name: Notebook name (with or without extension).

    Returns:
        Structured cell contents (type, source, truncated outputs).
    """

    path = notebook_path(name, NOTEBOOK_DIR)
    if not path.exists():
        # Try the other format if not found
        alt = path.with_suffix(".ipynb" if path.suffix == ".qmd" else ".qmd")
        if alt.exists():
            path = alt
        else:
            return {"error": f"Notebook '{name}' not found in {NOTEBOOK_DIR}"}

    nb = load_notebook(path)
    cells: list[dict[str, Any]] = []
    for i, cell in enumerate(nb.cells):
        entry: dict[str, Any] = {
            "index": i,
            "type": cell.cell_type,
            "source": cell.source[:2000] + ("..." if len(cell.source) > 2000 else ""),
        }
        if cell.cell_type == "code" and hasattr(cell, "outputs"):
            entry["output_summary"] = outputs_for_agent(cell.outputs)
        cells.append(entry)

    return {
        "notebook": path.name,
        "cells": cells,
        "cell_count": len(cells),
    }


@mcp.tool()  # type: ignore[untyped-decorator]
async def list_notebooks() -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """List saved notebooks in the notebook directory.

    Returns:
        List of notebook filenames with sizes and modification times.
    """

    notebooks: list[dict[str, Any]] = []
    for ext in ("*.ipynb", "*.qmd"):
        for path in sorted(NOTEBOOK_DIR.glob(ext)):
            stat = path.stat()
            notebooks.append(
                {
                    "name": path.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.datetime.fromtimestamp(
                        stat.st_mtime, tz=datetime.UTC
                    ).isoformat(),
                }
            )

    return {"notebooks": notebooks, "count": len(notebooks), "directory": str(NOTEBOOK_DIR)}


@mcp.tool()  # type: ignore[untyped-decorator]
async def list_kernels() -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """List active kernels and their notebook names.

    Returns:
        List of kernel sessions with IDs, notebook names, and cell counts.
    """

    kernels: list[dict[str, Any]] = []
    for kid, session in _sessions.items():
        kernels.append(
            {
                "kernel_id": kid,
                "notebook": notebook_path(session.name, NOTEBOOK_DIR).name,
                "cells": len(session.nb.cells),
            }
        )

    return {"kernels": kernels, "count": len(kernels)}


@mcp.tool()  # type: ignore[untyped-decorator]
async def shutdown_kernel(kernel_id: str) -> dict[str, Any]:  # noqa: RUF029 - FastMCP may await tools.
    """Save and shut down a kernel.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.

    Returns:
        Confirmation with final notebook name and cell count.
    """

    session = _sessions.pop(kernel_id, None)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'."}

    # Final save
    path = notebook_path(session.name, NOTEBOOK_DIR)
    save_notebook(session.nb, path)

    # Shutdown kernel
    session.client.stop_channels()
    session.km.shutdown_kernel(now=True)

    return {
        "notebook": path.name,
        "cells": len(session.nb.cells),
        "status": "shutdown",
    }


# ---------------------------------------------------------------------------
# JupyterLab subprocess
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _NotebookServerState:
    lab_process: subprocess.Popen[bytes] | None = None  # noqa: V107


_state = _NotebookServerState()


def _start_jupyterlab() -> None:
    """Start JupyterLab as a viewing frontend."""
    _state.lab_process = subprocess.Popen(  # noqa: S603, V101 - fixed python -m jupyterlab argv; no shell.
        [
            sys.executable,
            "-m",
            "jupyterlab",
            f"--ip={CONTAINER_BIND_HOST}",
            f"--port={ARGS.lab_port}",
            "--no-browser",
            f"--notebook-dir={NOTEBOOK_DIR}",
            "--IdentityProvider.token=''",
            "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _stdout_line(f"JupyterLab started on port {ARGS.lab_port}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _start_jupyterlab()

    _stdout_line(f"Notebook server starting on port {ARGS.port}")
    _stdout_line(f"Notebook directory: {NOTEBOOK_DIR}")
    _stdout_line(f"Workspace directory: {WORKSPACE_DIR}")

    mcp.run(transport="http", host=CONTAINER_BIND_HOST, port=ARGS.port)
