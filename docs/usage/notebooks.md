# Notebooks

The built-in notebook MCP server lets agents create, execute, and manage Jupyter notebooks and Quarto documents (.qmd). Agents interact through MCP tools; humans view results in JupyterLab.

## How it works

The notebook server runs as a host-side script MCP server (not in a container) and provides:

- **MCP tools** for agents — start kernels, execute code cells, add markdown, save/load notebooks
- **JupyterLab** for humans — a web frontend on port 8888 for viewing and interacting with notebooks
- **IPython kernels** managed directly via `jupyter_client` — no `jupyter_server` overhead

Agents interact with notebooks exclusively through MCP tools, which handle kernel lifecycle, cell execution, output collection, and auto-saving. Agents can also read and edit `.qmd` files directly from the workspace, since notebooks live in `groups/<workspace>/notebooks/` (mounted at `/workspace/group/notebooks/` inside the container).

## Enabling notebooks

Add `"notebook"` to a workspace's MCP server list:

```toml
[workspaces.research]
mcp_servers = ["notebook"]
```

No per-workspace config required. The server automatically scopes notebooks to `groups/<workspace>/notebooks/` and sets the kernel's working directory to `groups/<workspace>/`, so the agent can reference workspace files naturally (e.g., `pd.read_csv("mydata.csv")`).

Each workspace gets its own server instance — no cross-workspace contamination.

## Default format: Quarto (.qmd)

Notebooks default to **.qmd** (Quarto markdown) rather than .ipynb. Quarto documents use plain text with code fences, making them more readable for agents and easier to diff in version control:

````markdown
## Sales Analysis

Loaded the Q4 sales data and filtered for the US region.

```{python}
import pandas as pd
df = pd.read_csv("sales.csv")
df[df.region == "US"].head()
```

The US region accounts for 62% of total revenue.
````

To work with .ipynb files instead, include the extension in the notebook name (e.g., `start_kernel(name="analysis.ipynb")`).

## MCP tools

All tools become available once the workspace includes `"notebook"` in its server list.

### Kernel lifecycle

**`start_kernel(name?)`** — Start an IPython kernel. If `name` refers to an existing notebook, load and re-execute all code cells to restore kernel state (session rehydration). If omitted, generate a name like `2026-02-20-ailing-amoeba`.

**`shutdown_kernel(kernel_id)`** — Save and shut down a kernel.

**`list_kernels()`** — List active kernels with their notebook names and cell counts.

### Working with cells

**`execute_cell(kernel_id, code)`** — Execute Python code. Returns outputs (text, image file paths, errors). The cell and its outputs append to the notebook and auto-save to disk. Images save automatically to `<notebook>_files/` alongside the notebook.

**`add_markdown(kernel_id, content)`** — Add a markdown cell. Auto-saves to disk.

### File operations

**`save_as(kernel_id, name)`** — Save under a different name. Use `.qmd` or `.ipynb` extension to control format.

**`read_notebook(name)`** — Read an existing notebook without starting a kernel. Returns structured cell contents.

**`list_notebooks()`** — List saved notebooks with sizes and modification times.

## Agent workflow

A typical agent session:

```
start_kernel()
  → kernel_id: "a1b2c3d4", notebook: "2026-02-20-ailing-amoeba.qmd"

execute_cell(kernel_id="a1b2c3d4", code="import pandas as pd\ndf = pd.read_csv('sales.csv')\ndf.head()")
  → outputs: [{"type": "result", "text": "   date    revenue\n0  ..."}]

add_markdown(kernel_id="a1b2c3d4", content="## Sales Analysis\nLoaded sales data for Q4.")

execute_cell(kernel_id="a1b2c3d4", code="df.describe()")
  → outputs: [{"type": "result", "text": "       revenue\ncount  ..."}]

save_as(kernel_id="a1b2c3d4", name="q4-sales-analysis")
  → notebook: "q4-sales-analysis.qmd", cells: 4

shutdown_kernel(kernel_id="a1b2c3d4")
```

## Session rehydration

When an agent calls `start_kernel(name="q4-sales-analysis")` for an existing notebook, the server:

1. Starts a fresh IPython kernel
2. Loads the notebook from disk
3. Re-executes all code cells sequentially to restore kernel state
4. Returns a summary: cell count, any errors during replay

This lets agents resume work across sessions without losing state. The kernel starts fresh, but replaying the cells restores all variables and imports.

## Agent-friendly output

The kernel auto-configures libraries for text-friendly output at startup:

- **Pandas** — wide column display, increased row/column limits for readable tables
- **Matplotlib** — non-interactive `Agg` backend (avoids GUI window attempts)
- **Images** — all `image/png` outputs (matplotlib plots, PIL images, etc.) save automatically to `<notebook>_files/cell_N.png`. The agent receives the file path instead of raw base64.

## Direct file access

Since notebooks live inside the workspace folder (`/workspace/group/notebooks/`), agents can also:

- Read `.qmd` files from previous sessions directly with filesystem tools
- Edit earlier cells by modifying the `.qmd` file, then re-executing with `start_kernel(name=...)`
- Include notebook files in git commits

## Viewing notebooks

JupyterLab runs alongside the MCP server on port 8888 (no authentication — designed for Tailscale access). Open `http://pynchy-server:8888` to browse and interact with notebooks.

Notebooks auto-save on every `execute_cell` and `add_markdown` call, so JupyterLab always reflects the latest state.

## Idle timeout

The server shuts down after 30 minutes of no MCP tool calls. On shutdown, all notebooks save and all kernels shut down cleanly. The next agent tool call restarts the server automatically.

---

**Want to customize this?** Write your own plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
