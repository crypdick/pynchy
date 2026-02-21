# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jupyter-client>=8.6",
#     "ipykernel>=6.30",
#     "nbformat>=5.10",
#     "fastmcp>=2.10",
#     "pillow>=11.0",
#     "jupyterlab>=4.0",
#     "ubuntu-namer>=1.1",
# ]
# ///
"""Notebook execution MCP server — FastMCP + jupyter_client + JupyterLab.

Standalone PEP 723 uv script. Heavy dependencies (JupyterLab, IPython kernel,
FastMCP) are resolved ad-hoc by ``uv run`` and never touch pynchy's virtualenv.

Provides MCP tools for agents to create, execute, and manage Jupyter notebooks.
JupyterLab runs as a separate subprocess for human viewing via Tailscale.

Usage::

    uv run scripts/notebook_server.py
    uv run scripts/notebook_server.py --notebook_dir data/notebooks/research
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import nbformat
from fastmcp import FastMCP
from jupyter_client import KernelManager
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

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
        help="Workspace name — notebooks scoped to data/notebooks/<workspace>/",
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
_ROOT = _project_root()
# Notebooks live inside the workspace folder so the agent container can
# read/edit .qmd files directly (groups/<ws>/ mounts at /workspace/group/).
WORKSPACE_DIR = (_ROOT / "groups" / ARGS.workspace).resolve()
NOTEBOOK_DIR = (WORKSPACE_DIR / "notebooks").resolve()
NOTEBOOK_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("Notebook Server")


# ---------------------------------------------------------------------------
# Kernel session state
# ---------------------------------------------------------------------------


class KernelSession:
    """Tracks a running kernel and its associated notebook."""

    def __init__(self, kernel_id: str, km: KernelManager, client: Any, name: str):
        self.kernel_id = kernel_id
        self.km = km
        self.client = client  # must already have start_channels() called
        self.name = name  # notebook name (without extension)
        self.nb = new_notebook()
        self.nb.metadata["kernelspec"] = {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        }


# kernel_id -> KernelSession
_sessions: dict[str, KernelSession] = {}

# Idle timeout tracking
_last_activity: float = time.monotonic()
_IDLE_TIMEOUT_SECS = 30 * 60  # 30 minutes


def _touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


# ---------------------------------------------------------------------------
# Notebook name generation
# ---------------------------------------------------------------------------


def _generate_name() -> str:
    """Generate a notebook name: YYYY-MM-DD-adjective-animal."""
    from ubuntu_namer import generate

    today = datetime.date.today().isoformat()
    slug = generate()  # e.g. "ailing-amoeba"
    return f"{today}-{slug}"


def _notebook_path(name: str) -> Path:
    """Resolve notebook name to full path, adding .qmd if no extension."""
    if not name.endswith((".ipynb", ".qmd")):
        name = f"{name}.qmd"
    return NOTEBOOK_DIR / name


# ---------------------------------------------------------------------------
# .qmd parsing / serialization
# ---------------------------------------------------------------------------


def _parse_qmd(text: str) -> nbformat.NotebookNode:
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


def _serialize_qmd(nb: nbformat.NotebookNode) -> str:
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


def _load_notebook(path: Path) -> nbformat.NotebookNode:
    """Load a notebook from disk (.ipynb or .qmd)."""
    if path.suffix == ".qmd":
        return _parse_qmd(path.read_text())
    return nbformat.read(str(path), as_version=4)


def _save_notebook(nb: nbformat.NotebookNode, path: Path) -> None:
    """Save a notebook to disk (.ipynb or .qmd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".qmd":
        path.write_text(_serialize_qmd(nb))
    else:
        nbformat.write(nb, str(path))


# ---------------------------------------------------------------------------
# Kernel startup configuration
# ---------------------------------------------------------------------------

# Silently executed at kernel start — not saved to the notebook.
# Configures libraries for agent-friendly text output.
_KERNEL_STARTUP_CODE = """\
import warnings as _w; _w.filterwarnings("ignore", category=FutureWarning)

# Pandas: text-friendly defaults (no HTML, wide columns, markdown tables)
try:
    import pandas as _pd
    _pd.set_option("display.max_columns", 50)
    _pd.set_option("display.max_colwidth", 80)
    _pd.set_option("display.width", 200)
    _pd.set_option("display.max_rows", 60)
except ImportError:
    pass

# Matplotlib: non-interactive backend (avoids GUI window attempts)
try:
    import matplotlib as _mpl
    _mpl.use("Agg")
except ImportError:
    pass

del _w
"""


# ---------------------------------------------------------------------------
# Image saving
# ---------------------------------------------------------------------------


def _image_dir(session_name: str) -> Path:
    """Directory for saved images: notebooks/<name>_files/."""
    d = NOTEBOOK_DIR / f"{session_name}_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_cell_images(
    session_name: str,
    cell_number: int,
    outputs: list[dict[str, Any]],
) -> None:
    """Extract image/png data from outputs and save to disk.

    Mutates outputs in-place: adds ``_image_path`` to data dicts that
    contain ``image/png``. Only creates the images directory when there
    are actual images to save.
    """
    import base64

    img_dir: Path | None = None
    img_count = 0

    for out in outputs:
        data = out.get("data", {})
        if "image/png" not in data:
            continue

        # Lazy-create directory on first image
        if img_dir is None:
            img_dir = _image_dir(session_name)

        img_count += 1
        suffix = f"_{img_count}" if img_count > 1 else ""
        filename = f"cell_{cell_number}{suffix}.png"
        filepath = img_dir / filename

        png_bytes = base64.b64decode(data["image/png"])
        filepath.write_bytes(png_bytes)

        # Add file path — keep original base64 in notebook for JupyterLab rendering
        data["_image_path"] = str(filepath.relative_to(NOTEBOOK_DIR))


# ---------------------------------------------------------------------------
# Kernel execution helpers
# ---------------------------------------------------------------------------


async def _execute_code(session: KernelSession, code: str) -> list[dict[str, Any]]:
    """Execute code on a kernel and collect outputs.

    Returns a list of output dicts matching nbformat output schema, suitable
    for both returning to the agent and storing in the notebook cell.
    """
    client = session.client
    msg_id = client.execute(code)
    outputs: list[dict[str, Any]] = []

    while True:
        try:
            msg = await asyncio.wait_for(
                asyncio.to_thread(client.get_iopub_msg, timeout=300),
                timeout=310,
            )
        except TimeoutError:
            outputs.append({
                "output_type": "error",
                "ename": "Timeout",
                "evalue": "Cell execution timed out (5 min)",
                "traceback": [],
            })
            break

        if msg["parent_header"].get("msg_id") != msg_id:
            continue

        msg_type = msg["msg_type"]
        content = msg["content"]

        if msg_type == "status" and content.get("execution_state") == "idle":
            break

        if msg_type == "stream":
            outputs.append({
                "output_type": "stream",
                "name": content.get("name", "stdout"),
                "text": content.get("text", ""),
            })
        elif msg_type in ("execute_result", "display_data"):
            output: dict[str, Any] = {
                "output_type": msg_type,
                "data": content.get("data", {}),
                "metadata": content.get("metadata", {}),
            }
            if msg_type == "execute_result":
                output["execution_count"] = content.get("execution_count")
            outputs.append(output)
        elif msg_type == "error":
            outputs.append({
                "output_type": "error",
                "ename": content.get("ename", ""),
                "evalue": content.get("evalue", ""),
                "traceback": content.get("traceback", []),
            })

    return outputs


def _outputs_for_agent(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simplify outputs for agent consumption.

    Truncates large text, converts image data to summaries (the agent doesn't
    need raw base64), and flattens stream outputs.
    """
    MAX_TEXT = 8000
    result: list[dict[str, Any]] = []

    for out in outputs:
        otype = out.get("output_type")
        if otype == "stream":
            text = out.get("text", "")
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + f"\n... (truncated, {len(out['text'])} chars total)"
            result.append({"type": "stream", "name": out.get("name"), "text": text})

        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {})
            entry: dict[str, Any] = {"type": "result" if otype == "execute_result" else "display"}

            # Prefer text/plain for agent readability
            if "text/plain" in data:
                text = data["text/plain"]
                if len(text) > MAX_TEXT:
                    text = text[:MAX_TEXT] + "\n... (truncated)"
                entry["text"] = text

            # Image: use saved file path if available, otherwise note presence
            if "image/png" in data:
                if "_image_path" in data:
                    entry["image_path"] = data["_image_path"]
                else:
                    entry["has_image"] = True

            result.append(entry)

        elif otype == "error":
            tb = out.get("traceback", [])
            # ANSI-strip traceback for readability
            tb_text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", line) for line in tb)
            if len(tb_text) > MAX_TEXT:
                tb_text = tb_text[:MAX_TEXT] + "\n... (truncated)"
            result.append({
                "type": "error",
                "ename": out.get("ename"),
                "evalue": out.get("evalue"),
                "traceback": tb_text,
            })

    return result


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_kernel(name: str | None = None) -> dict[str, Any]:
    """Start an IPython kernel, optionally loading an existing notebook.

    If ``name`` refers to an existing notebook on disk, loads it and
    re-executes all code cells to restore kernel state. If omitted, creates
    a new notebook with an auto-generated name.

    Args:
        name: Notebook name (without path). If the notebook exists on disk,
              its cells are re-executed to restore state. If omitted, a new
              name is auto-generated.

    Returns:
        Session info: kernel_id, notebook name, cell summary (if rehydrating).
    """
    _touch_activity()

    if name is None:
        name = _generate_name()

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
    await _execute_code(session, _KERNEL_STARTUP_CODE)

    result: dict[str, Any] = {
        "kernel_id": kernel_id,
        "notebook": _notebook_path(base_name).name,
        "status": "started",
    }

    # Rehydrate if notebook exists on disk
    nb_path = _notebook_path(base_name)
    if nb_path.exists():
        existing_nb = _load_notebook(nb_path)
        session.nb = existing_nb
        errors: list[str] = []
        code_count = 0
        for cell in existing_nb.cells:
            if cell.cell_type == "code":
                code_count += 1
                outputs = await _execute_code(session, cell.source)
                cell.outputs = outputs
                for out in outputs:
                    if out.get("output_type") == "error":
                        errors.append(f"Cell {code_count}: {out.get('ename')}: {out.get('evalue')}")

        result["rehydrated"] = True
        result["cells_total"] = len(existing_nb.cells)
        result["code_cells_executed"] = code_count
        if errors:
            result["replay_errors"] = errors
        result["status"] = "rehydrated"
    else:
        result["rehydrated"] = False

    return result


@mcp.tool()
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
    _touch_activity()

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    outputs = await _execute_code(session, code)

    # Append cell + outputs to notebook
    cell = new_code_cell(source=code)
    cell.outputs = outputs
    session.nb.cells.append(cell)
    cell_number = len(session.nb.cells)

    # Save images to disk (mutates outputs in-place with _image_path)
    _save_cell_images(session.name, cell_number, outputs)

    # Auto-save
    _save_notebook(session.nb, _notebook_path(session.name))

    return {
        "cell_number": cell_number,
        "outputs": _outputs_for_agent(outputs),
    }


@mcp.tool()
async def add_markdown(kernel_id: str, content: str) -> dict[str, Any]:
    """Add a markdown cell to the notebook.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.
        content: Markdown text for the cell.

    Returns:
        Confirmation with cell number.
    """
    _touch_activity()

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    session.nb.cells.append(new_markdown_cell(source=content))
    _save_notebook(session.nb, _notebook_path(session.name))

    return {
        "cell_number": len(session.nb.cells),
        "notebook": _notebook_path(session.name).name,
    }


@mcp.tool()
async def save_as(kernel_id: str, name: str) -> dict[str, Any]:
    """Save the current notebook under a different name.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.
        name: New notebook name (without path).

    Returns:
        Confirmation with new filename and cell count.
    """
    _touch_activity()

    session = _sessions.get(kernel_id)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'. Use start_kernel first."}

    base_name = name.removesuffix(".ipynb").removesuffix(".qmd")
    ext = ".ipynb" if name.endswith(".ipynb") else ".qmd"
    new_path = NOTEBOOK_DIR / f"{base_name}{ext}"

    _save_notebook(session.nb, new_path)
    session.name = base_name

    return {
        "notebook": new_path.name,
        "cells": len(session.nb.cells),
    }


@mcp.tool()
async def read_notebook(name: str) -> dict[str, Any]:
    """Read an existing notebook without starting a kernel.

    Args:
        name: Notebook name (with or without extension).

    Returns:
        Structured cell contents (type, source, truncated outputs).
    """
    _touch_activity()

    path = _notebook_path(name)
    if not path.exists():
        # Try the other format if not found
        alt = path.with_suffix(".ipynb" if path.suffix == ".qmd" else ".qmd")
        if alt.exists():
            path = alt
        else:
            return {"error": f"Notebook '{name}' not found in {NOTEBOOK_DIR}"}

    nb = _load_notebook(path)
    cells: list[dict[str, Any]] = []
    for i, cell in enumerate(nb.cells):
        entry: dict[str, Any] = {
            "index": i,
            "type": cell.cell_type,
            "source": cell.source[:2000] + ("..." if len(cell.source) > 2000 else ""),
        }
        if cell.cell_type == "code" and hasattr(cell, "outputs"):
            entry["output_summary"] = _outputs_for_agent(cell.outputs)
        cells.append(entry)

    return {
        "notebook": path.name,
        "cells": cells,
        "cell_count": len(cells),
    }


@mcp.tool()
async def list_notebooks() -> dict[str, Any]:
    """List saved notebooks in the notebook directory.

    Returns:
        List of notebook filenames with sizes and modification times.
    """
    _touch_activity()

    notebooks: list[dict[str, Any]] = []
    for ext in ("*.ipynb", "*.qmd"):
        for path in sorted(NOTEBOOK_DIR.glob(ext)):
            stat = path.stat()
            notebooks.append({
                "name": path.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    return {"notebooks": notebooks, "count": len(notebooks), "directory": str(NOTEBOOK_DIR)}


@mcp.tool()
async def list_kernels() -> dict[str, Any]:
    """List active kernels and their notebook names.

    Returns:
        List of kernel sessions with IDs, notebook names, and cell counts.
    """
    _touch_activity()

    kernels: list[dict[str, Any]] = []
    for kid, session in _sessions.items():
        kernels.append({
            "kernel_id": kid,
            "notebook": _notebook_path(session.name).name,
            "cells": len(session.nb.cells),
        })

    return {"kernels": kernels, "count": len(kernels)}


@mcp.tool()
async def shutdown_kernel(kernel_id: str) -> dict[str, Any]:
    """Save and shut down a kernel.

    Args:
        kernel_id: Kernel identifier from ``start_kernel``.

    Returns:
        Confirmation with final notebook name and cell count.
    """
    _touch_activity()

    session = _sessions.pop(kernel_id, None)
    if not session:
        return {"error": f"No active kernel with id '{kernel_id}'."}

    # Final save
    path = _notebook_path(session.name)
    _save_notebook(session.nb, path)

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

_lab_process = None


def _start_jupyterlab() -> None:
    """Start JupyterLab as a viewing frontend."""
    import subprocess

    global _lab_process
    _lab_process = subprocess.Popen(
        [
            sys.executable, "-m", "jupyterlab",
            "--ip=0.0.0.0",
            f"--port={ARGS.lab_port}",
            "--no-browser",
            f"--notebook-dir={NOTEBOOK_DIR}",
            "--IdentityProvider.token=''",
            "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"JupyterLab started on port {ARGS.lab_port}", flush=True)


def _stop_jupyterlab() -> None:
    global _lab_process
    if _lab_process and _lab_process.poll() is None:
        _lab_process.terminate()
        try:
            _lab_process.wait(timeout=10)
        except Exception:
            _lab_process.kill()
    _lab_process = None


# ---------------------------------------------------------------------------
# Idle timeout
# ---------------------------------------------------------------------------


def _idle_watchdog() -> None:
    """Daemon thread: check for idle timeout, save state, and exit."""
    while True:
        time.sleep(60)
        elapsed = time.monotonic() - _last_activity
        if elapsed > _IDLE_TIMEOUT_SECS:
            print(f"Idle timeout ({_IDLE_TIMEOUT_SECS}s) reached. Shutting down.", flush=True)
            _graceful_shutdown()
            return


def _graceful_shutdown() -> None:
    """Save all notebooks, shutdown all kernels, stop JupyterLab, exit."""
    for kid in list(_sessions):
        session = _sessions.pop(kid)
        _save_notebook(session.nb, _notebook_path(session.name))
        try:
            session.client.stop_channels()
            session.km.shutdown_kernel(now=True)
        except Exception:
            pass  # best-effort cleanup
    _stop_jupyterlab()
    os._exit(0)  # hard exit from daemon thread


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _start_jupyterlab()

    # Idle watchdog runs in a daemon thread — dies with the main process
    watchdog = threading.Thread(target=_idle_watchdog, daemon=True)
    watchdog.start()

    print(f"Notebook server starting on port {ARGS.port}", flush=True)
    print(f"Notebook directory: {NOTEBOOK_DIR}", flush=True)
    print(f"Workspace directory: {WORKSPACE_DIR}", flush=True)

    mcp.run(transport="http", host="0.0.0.0", port=ARGS.port)
