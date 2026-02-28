"""Kernel session state and code execution.

``KernelSession`` tracks a running kernel and its associated notebook.
``execute_code`` sends code to a kernel and collects outputs in nbformat schema.

The ``KernelManager`` type is only imported under ``TYPE_CHECKING`` so this
module stays importable without jupyter_client at type-check time.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nbformat.v4 import new_notebook

if TYPE_CHECKING:
    from jupyter_client import KernelManager


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


# Silently executed at kernel start — not saved to the notebook.
# Configures libraries for agent-friendly text output.
KERNEL_STARTUP_CODE = """\
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


async def execute_code(session: KernelSession, code: str) -> list[dict[str, Any]]:
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
            outputs.append(
                {
                    "output_type": "error",
                    "ename": "Timeout",
                    "evalue": "Cell execution timed out (5 min)",
                    "traceback": [],
                }
            )
            break

        if msg["parent_header"].get("msg_id") != msg_id:
            continue

        msg_type = msg["msg_type"]
        content = msg["content"]

        if msg_type == "status" and content.get("execution_state") == "idle":
            break

        if msg_type == "stream":
            outputs.append(
                {
                    "output_type": "stream",
                    "name": content.get("name", "stdout"),
                    "text": content.get("text", ""),
                }
            )
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
            outputs.append(
                {
                    "output_type": "error",
                    "ename": content.get("ename", ""),
                    "evalue": content.get("evalue", ""),
                    "traceback": content.get("traceback", []),
                }
            )

    return outputs
