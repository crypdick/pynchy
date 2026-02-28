"""Tests for notebook .qmd/.ipynb format parsing, serialization, and round-trips.

Functions are imported directly from the ``notebook_server`` package submodules.
No sys.argv patching needed — the submodules have no module-level side effects.
"""

from __future__ import annotations

import nbformat
import pytest
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

try:
    from pynchy.plugins.integrations.notebook_server._formats import (
        load_notebook,
        notebook_path,
        parse_qmd,
        save_notebook,
        serialize_qmd,
    )
    from pynchy.plugins.integrations.notebook_server._output import (
        outputs_for_agent,
        save_cell_images,
    )
except ImportError:
    pytest.skip("notebook_server deps not installed", allow_module_level=True)


# ---------------------------------------------------------------------------
# .qmd parsing
# ---------------------------------------------------------------------------


class TestParseQmd:
    """Tests for parse_qmd: .qmd text -> notebook node."""

    def test_empty_document(self):
        nb = parse_qmd("")
        assert len(nb.cells) == 0

    def test_markdown_only(self):
        text = "# Title\n\nSome text about the analysis."
        nb = parse_qmd(text)
        assert len(nb.cells) == 1
        assert nb.cells[0].cell_type == "markdown"
        assert "Title" in nb.cells[0].source

    def test_code_only(self):
        text = "```{python}\nprint('hello')\n```"
        nb = parse_qmd(text)
        assert len(nb.cells) == 1
        assert nb.cells[0].cell_type == "code"
        assert nb.cells[0].source == "print('hello')"

    def test_mixed_cells(self):
        text = (
            "# Analysis\n\nIntroduction.\n\n"
            "```{python}\nimport pandas as pd\ndf = pd.read_csv('data.csv')\n```\n\n"
            "## Results\n\nThe data shows...\n\n"
            "```{python}\ndf.describe()\n```"
        )
        nb = parse_qmd(text)
        assert len(nb.cells) == 4
        assert nb.cells[0].cell_type == "markdown"
        assert nb.cells[1].cell_type == "code"
        assert nb.cells[2].cell_type == "markdown"
        assert nb.cells[3].cell_type == "code"
        assert "import pandas" in nb.cells[1].source
        assert "df.describe()" in nb.cells[3].source

    def test_code_fence_options_ignored(self):
        """Code fences like ```{python} #| echo: false should still parse."""
        text = "```{python}\n#| echo: false\nprint('hi')\n```"
        nb = parse_qmd(text)
        assert len(nb.cells) == 1
        assert nb.cells[0].cell_type == "code"
        assert "#| echo: false" in nb.cells[0].source

    def test_multiline_code(self):
        code = "x = 1\ny = 2\nz = x + y\nprint(z)"
        text = f"```{{python}}\n{code}\n```"
        nb = parse_qmd(text)
        assert nb.cells[0].source == code

    def test_empty_code_cell(self):
        text = "```{python}\n```"
        nb = parse_qmd(text)
        assert len(nb.cells) == 1
        assert nb.cells[0].cell_type == "code"
        assert nb.cells[0].source == ""

    def test_consecutive_code_cells(self):
        text = "```{python}\na = 1\n```\n\n```{python}\nb = 2\n```"
        nb = parse_qmd(text)
        code_cells = [c for c in nb.cells if c.cell_type == "code"]
        assert len(code_cells) == 2
        assert code_cells[0].source == "a = 1"
        assert code_cells[1].source == "b = 2"

    def test_whitespace_only_markdown_skipped(self):
        text = "\n\n\n```{python}\nprint(1)\n```\n\n\n"
        nb = parse_qmd(text)
        # Should only have the code cell — whitespace-only markdown is skipped
        code_cells = [c for c in nb.cells if c.cell_type == "code"]
        assert len(code_cells) == 1

    def test_kernelspec_metadata(self):
        nb = parse_qmd("```{python}\npass\n```")
        assert nb.metadata["kernelspec"]["name"] == "python3"


# ---------------------------------------------------------------------------
# .qmd serialization
# ---------------------------------------------------------------------------


class TestSerializeQmd:
    """Tests for serialize_qmd: notebook node -> .qmd text."""

    def test_empty_notebook(self):
        nb = new_notebook()
        text = serialize_qmd(nb)
        assert text.strip() == ""

    def test_markdown_cell(self):
        nb = new_notebook()
        nb.cells.append(new_markdown_cell(source="# Hello"))
        text = serialize_qmd(nb)
        assert "# Hello" in text

    def test_code_cell(self):
        nb = new_notebook()
        nb.cells.append(new_code_cell(source="print('hi')"))
        text = serialize_qmd(nb)
        assert "```{python}" in text
        assert "print('hi')" in text
        assert text.count("```") == 2  # opening and closing

    def test_mixed_cells(self):
        nb = new_notebook()
        nb.cells.append(new_markdown_cell(source="# Title"))
        nb.cells.append(new_code_cell(source="x = 1"))
        nb.cells.append(new_markdown_cell(source="## Section"))
        nb.cells.append(new_code_cell(source="y = 2"))
        text = serialize_qmd(nb)

        lines = text.split("\n")
        assert "# Title" in lines
        assert "## Section" in lines
        assert text.count("```{python}") == 2


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------


class TestQmdRoundTrip:
    """Verify that qmd -> notebook -> qmd preserves content."""

    def test_simple_round_trip(self):
        original = (
            "# My Analysis\n\n"
            "Some intro text.\n\n"
            "```{python}\nimport pandas as pd\ndf = pd.DataFrame({'a': [1,2,3]})\n```\n\n"
            "## Results\n\n"
            "```{python}\ndf.describe()\n```\n"
        )
        nb = parse_qmd(original)
        result = serialize_qmd(nb)

        # Re-parse should produce the same structure
        nb2 = parse_qmd(result)
        assert len(nb.cells) == len(nb2.cells)
        for c1, c2 in zip(nb.cells, nb2.cells, strict=True):
            assert c1.cell_type == c2.cell_type
            assert c1.source.strip() == c2.source.strip()

    def test_code_content_preserved_exactly(self):
        """Code cell content must survive round-trip without mutation."""
        code = "def foo(x):\n    return x * 2\n\nresult = foo(21)\nprint(result)"
        original = f"```{{python}}\n{code}\n```\n"
        nb = parse_qmd(original)
        assert nb.cells[0].source == code

        result = serialize_qmd(nb)
        nb2 = parse_qmd(result)
        assert nb2.cells[0].source == code


# ---------------------------------------------------------------------------
# Notebook I/O (.ipynb and .qmd)
# ---------------------------------------------------------------------------


class TestNotebookIO:
    """Tests for load_notebook and save_notebook file I/O."""

    def test_save_and_load_ipynb(self, tmp_path):
        nb = new_notebook()
        nb.cells.append(new_code_cell(source="print('hello')"))
        nb.cells.append(new_markdown_cell(source="# Title"))
        path = tmp_path / "test.ipynb"

        save_notebook(nb, path)
        assert path.exists()

        loaded = load_notebook(path)
        assert len(loaded.cells) == 2
        assert loaded.cells[0].cell_type == "code"
        assert loaded.cells[0].source == "print('hello')"

    def test_save_and_load_qmd(self, tmp_path):
        nb = new_notebook()
        nb.cells.append(new_markdown_cell(source="# Analysis"))
        nb.cells.append(new_code_cell(source="x = 42"))
        path = tmp_path / "test.qmd"

        save_notebook(nb, path)
        assert path.exists()
        # .qmd is plain text — verify it's readable
        text = path.read_text()
        assert "# Analysis" in text
        assert "```{python}" in text

        loaded = load_notebook(path)
        assert len(loaded.cells) == 2
        assert loaded.cells[0].cell_type == "markdown"
        assert loaded.cells[1].cell_type == "code"
        assert loaded.cells[1].source == "x = 42"

    def test_ipynb_is_valid_nbformat(self, tmp_path):
        """Saved .ipynb files must pass nbformat validation."""
        nb = new_notebook()
        nb.metadata["kernelspec"] = {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        }
        nb.cells.append(new_code_cell(source="1 + 1"))
        path = tmp_path / "valid.ipynb"

        save_notebook(nb, path)
        loaded = nbformat.read(str(path), as_version=4)
        nbformat.validate(loaded)  # raises on invalid

    def test_qmd_to_ipynb_conversion(self, tmp_path):
        """Load a .qmd file, save as .ipynb, verify valid notebook."""
        qmd_path = tmp_path / "source.qmd"
        qmd_path.write_text("# Title\n\n```{python}\nprint('converted')\n```\n")

        nb = load_notebook(qmd_path)
        ipynb_path = tmp_path / "converted.ipynb"
        save_notebook(nb, ipynb_path)

        loaded = nbformat.read(str(ipynb_path), as_version=4)
        nbformat.validate(loaded)
        assert len(loaded.cells) == 2
        assert loaded.cells[1].source == "print('converted')"

    def test_save_creates_parent_dirs(self, tmp_path):
        nb = new_notebook()
        nb.cells.append(new_code_cell(source="pass"))
        path = tmp_path / "deep" / "nested" / "dir" / "nb.qmd"

        save_notebook(nb, path)
        assert path.exists()


# ---------------------------------------------------------------------------
# notebook_path resolution
# ---------------------------------------------------------------------------


class TestNotebookPath:
    """Tests for notebook_path name resolution."""

    def test_bare_name_gets_qmd_extension(self, tmp_path):
        path = notebook_path("my-analysis", tmp_path)
        assert path.name == "my-analysis.qmd"

    def test_ipynb_extension_preserved(self, tmp_path):
        path = notebook_path("legacy.ipynb", tmp_path)
        assert path.name == "legacy.ipynb"

    def test_qmd_extension_preserved(self, tmp_path):
        path = notebook_path("doc.qmd", tmp_path)
        assert path.name == "doc.qmd"

    def test_path_under_notebook_dir(self, tmp_path):
        path = notebook_path("my-analysis", tmp_path)
        assert path.parent == tmp_path


# ---------------------------------------------------------------------------
# Output simplification
# ---------------------------------------------------------------------------


class TestOutputsForAgent:
    """Tests for outputs_for_agent output simplification."""

    def test_stream_output(self):
        outputs = [{"output_type": "stream", "name": "stdout", "text": "hello\n"}]
        result = outputs_for_agent(outputs)
        assert len(result) == 1
        assert result[0]["type"] == "stream"
        assert result[0]["text"] == "hello\n"

    def test_execute_result(self):
        outputs = [
            {
                "output_type": "execute_result",
                "data": {"text/plain": "42"},
                "metadata": {},
                "execution_count": 1,
            }
        ]
        result = outputs_for_agent(outputs)
        assert result[0]["type"] == "result"
        assert result[0]["text"] == "42"

    def test_image_without_path_shows_has_image(self):
        import base64

        fake_png = base64.b64encode(b"fake image data").decode()
        outputs = [
            {
                "output_type": "display_data",
                "data": {"image/png": fake_png, "text/plain": "<Figure>"},
                "metadata": {},
            }
        ]
        result = outputs_for_agent(outputs)
        assert result[0]["has_image"] is True
        # Base64 data should NOT be in the result
        assert fake_png not in str(result)

    def test_image_with_path_shows_path(self):
        import base64

        fake_png = base64.b64encode(b"fake image data").decode()
        outputs = [
            {
                "output_type": "display_data",
                "data": {
                    "image/png": fake_png,
                    "text/plain": "<Figure>",
                    "_image_path": "analysis_files/cell_1.png",
                },
                "metadata": {},
            }
        ]
        result = outputs_for_agent(outputs)
        assert result[0]["image_path"] == "analysis_files/cell_1.png"
        assert "has_image" not in result[0]

    def test_error_output_strips_ansi(self):
        outputs = [
            {
                "output_type": "error",
                "ename": "ValueError",
                "evalue": "bad value",
                "traceback": ["\x1b[31mValueError\x1b[0m: bad value"],
            }
        ]
        result = outputs_for_agent(outputs)
        assert result[0]["type"] == "error"
        assert "\x1b[" not in result[0]["traceback"]
        assert "ValueError" in result[0]["traceback"]

    def test_html_not_included(self):
        """HTML output should be dropped — agents get text/plain only."""
        outputs = [
            {
                "output_type": "execute_result",
                "data": {
                    "text/plain": "   a  b\n0  1  2",
                    "text/html": "<table><tr><td>1</td></tr></table>",
                },
                "metadata": {},
                "execution_count": 1,
            }
        ]
        result = outputs_for_agent(outputs)
        assert "html" not in result[0]
        assert result[0]["text"] == "   a  b\n0  1  2"

    def test_long_text_truncated(self):
        long_text = "x" * 10000
        outputs = [{"output_type": "stream", "name": "stdout", "text": long_text}]
        result = outputs_for_agent(outputs)
        assert len(result[0]["text"]) < len(long_text)
        assert "truncated" in result[0]["text"]


# ---------------------------------------------------------------------------
# Image saving
# ---------------------------------------------------------------------------


class TestSaveCellImages:
    """Tests for save_cell_images: extract PNG data and write to disk."""

    def test_saves_png_to_disk(self, tmp_path):
        import base64

        png_data = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        outputs = [
            {
                "output_type": "display_data",
                "data": {"image/png": png_data, "text/plain": "<Figure>"},
                "metadata": {},
            }
        ]

        save_cell_images("test-nb", 3, outputs, tmp_path)

        # File should exist
        img_path = tmp_path / "test-nb_files" / "cell_3.png"
        assert img_path.exists()
        assert img_path.read_bytes() == base64.b64decode(png_data)

        # Output should have _image_path added
        assert outputs[0]["data"]["_image_path"] == "test-nb_files/cell_3.png"

    def test_no_images_is_noop(self, tmp_path):
        outputs = [{"output_type": "stream", "name": "stdout", "text": "hi"}]
        save_cell_images("test-nb", 1, outputs, tmp_path)

        # No files directory should be created for non-image outputs
        assert not (tmp_path / "test-nb_files").exists()

    def test_multiple_images_numbered(self, tmp_path):
        import base64

        png1 = base64.b64encode(b"img1").decode()
        png2 = base64.b64encode(b"img2").decode()
        outputs = [
            {"output_type": "display_data", "data": {"image/png": png1}, "metadata": {}},
            {"output_type": "display_data", "data": {"image/png": png2}, "metadata": {}},
        ]

        save_cell_images("multi", 1, outputs, tmp_path)

        assert (tmp_path / "multi_files" / "cell_1.png").exists()
        assert (tmp_path / "multi_files" / "cell_1_2.png").exists()
