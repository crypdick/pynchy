"""Output processing for agent consumption and image saving.

All functions use stdlib only (plus ``base64`` for image decoding).
Functions that need a notebook directory accept it as an explicit parameter.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any


def _truncate_text(text: str, *, max_text: int, suffix: str) -> str:
    if len(text) <= max_text:
        return text
    return text[:max_text] + suffix


def _stream_output_entry(out: dict[str, Any], *, max_text: int) -> dict[str, Any]:
    text = str(out.get("text", ""))
    return {
        "type": "stream",
        "name": out.get("name"),
        "text": _truncate_text(
            text,
            max_text=max_text,
            suffix=f"\n... (truncated, {len(text)} chars total)",
        ),
    }


def _display_output_entry(out: dict[str, Any], *, max_text: int) -> dict[str, Any]:
    data = out.get("data", {})
    entry: dict[str, Any] = {
        "type": "result" if out.get("output_type") == "execute_result" else "display"
    }
    text = data.get("text/plain")
    if isinstance(text, str):
        entry["text"] = _truncate_text(text, max_text=max_text, suffix="\n... (truncated)")
    if "image/png" in data:
        if "_image_path" in data:
            entry["image_path"] = data["_image_path"]
        else:
            entry["has_image"] = True
    return entry


def _error_output_entry(out: dict[str, Any], *, max_text: int) -> dict[str, Any]:
    traceback_lines = out.get("traceback", [])
    tb_text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", line) for line in traceback_lines)
    return {
        "type": "error",
        "ename": out.get("ename"),
        "evalue": out.get("evalue"),
        "traceback": _truncate_text(tb_text, max_text=max_text, suffix="\n... (truncated)"),
    }


def outputs_for_agent(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simplify outputs for agent consumption.

    Truncates large text, converts image data to summaries (the agent doesn't
    need raw base64), and flattens stream outputs.
    """
    max_text = 8000
    result: list[dict[str, Any]] = []

    for out in outputs:
        otype = out.get("output_type")
        if otype == "stream":
            result.append(_stream_output_entry(out, max_text=max_text))
            continue
        if otype in ("execute_result", "display_data"):
            result.append(_display_output_entry(out, max_text=max_text))
            continue
        if otype == "error":
            result.append(_error_output_entry(out, max_text=max_text))

    return result


def image_dir(session_name: str, notebook_dir: Path) -> Path:
    """Directory for saved images: notebooks/<name>_files/."""
    d = notebook_dir / f"{session_name}_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_cell_images(
    session_name: str,
    cell_number: int,
    outputs: list[dict[str, Any]],
    notebook_dir: Path,
) -> None:
    """Extract image/png data from outputs and save to disk.

    Mutates outputs in-place: adds ``_image_path`` to data dicts that
    contain ``image/png``. Only creates the images directory when there
    are actual images to save.
    """
    img_dir: Path | None = None
    img_count = 0

    for out in outputs:
        data = out.get("data", {})
        if "image/png" not in data:
            continue

        # Lazy-create directory on first image
        if img_dir is None:
            img_dir = image_dir(session_name, notebook_dir)

        img_count += 1
        suffix = f"_{img_count}" if img_count > 1 else ""
        filename = f"cell_{cell_number}{suffix}.png"
        filepath = img_dir / filename

        png_bytes = base64.b64decode(data["image/png"])
        filepath.write_bytes(png_bytes)

        # Add file path — keep the base64 in the notebook for JupyterLab rendering
        data["_image_path"] = str(filepath.relative_to(notebook_dir))
