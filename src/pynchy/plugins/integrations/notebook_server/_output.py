"""Output processing for agent consumption and image saving.

All functions use stdlib only (plus ``base64`` for image decoding).
Functions that need a notebook directory accept it as an explicit parameter.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any


def outputs_for_agent(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            result.append(
                {
                    "type": "error",
                    "ename": out.get("ename"),
                    "evalue": out.get("evalue"),
                    "traceback": tb_text,
                }
            )

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

        # Add file path — keep original base64 in notebook for JupyterLab rendering
        data["_image_path"] = str(filepath.relative_to(notebook_dir))
