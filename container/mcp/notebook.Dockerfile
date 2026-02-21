# Notebook MCP server — Jupyter kernel execution in a sandboxed container.
# Build: docker build -t pynchy-mcp-notebook -f container/mcp/notebook.Dockerfile .
FROM python:3.13-slim

# Install uv so agents can `uv pip install --system <pkg>` from notebook cells.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install notebook server dependencies (matches pyproject.toml [notebook] extra).
RUN uv pip install --system \
    "fastmcp>=2.10" \
    "ipykernel>=6.30" \
    "jupyter-client>=8.6" \
    "jupyterlab>=4.0" \
    "nbformat>=5.10" \
    "pillow>=11.0" \
    "ubuntu-namer>=1.1"

# Common data-science libraries that agents frequently need.
RUN uv pip install --system \
    "pandas>=2.0" \
    "matplotlib>=3.8" \
    "numpy>=1.26"

# Copy only the notebook_server package — no pynchy imports needed.
COPY src/pynchy/integrations/plugins/notebook_server /app/notebook_server

WORKDIR /app
EXPOSE 8460 8888
ENTRYPOINT ["python", "-m", "notebook_server"]
