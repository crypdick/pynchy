# Pynchy Agent Container
# Runs agent cores (Claude SDK, OpenAI Agents SDK) in isolated Linux VM

FROM python:3.13-slim

# Install uv (available to all containers for running Python tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install system dependencies + common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ripgrep \
    jq \
    procps \
    gpg \
    libmagic1 \
    ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends gh nodejs \
    && apt-get purge -y gpg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Disable Python stdout buffering so streaming markers reach the host immediately
ENV PYTHONUNBUFFERED=1

# Install claude-code globally
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# Create app directory
WORKDIR /app

# Copy Python agent runner and install with uv
COPY agent_runner/ /app/
RUN uv pip install --system --no-cache-dir /app/

# Install pre-commit so git hooks work inside agent workspaces
RUN uv pip install --system --no-cache-dir pre-commit

# Install custom plugin dependencies (users add packages to this file)
COPY requirements-plugins.txt /app/requirements-plugins.txt
RUN uv pip install --system --no-cache-dir -r /app/requirements-plugins.txt

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create entrypoint script
# Sources env from mounted /workspace/env-dir/env if it exists (set -a exports all vars)
# Stdin is buffered to /tmp then piped (Apple Container requires EOF to flush stdin pipe)
RUN printf '#!/bin/bash\nset -e\nif [ -f /workspace/env-dir/env ]; then set -a; . /workspace/env-dir/env; set +a; fi\ncat > /tmp/input.json\npython -m agent_runner < /tmp/input.json\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Create non-root user for security
# NOTE: Update docs/security.md § Container Isolation if you change the username.
RUN useradd -m -s /bin/bash agent
RUN chown -R agent:agent /workspace

# Switch to non-root user
USER agent

# Delegate git authentication to gh CLI (uses GH_TOKEN from environment at runtime)
# Rewrite SSH remote URLs to HTTPS so the credential helper works (no SSH keys in container)
RUN git config --global credential.helper '!gh auth git-credential' \
 && git config --global url."https://github.com/".insteadOf "git@github.com:"

# Set working directory to group workspace
WORKDIR /workspace/group

# Entry point reads JSON from stdin, outputs JSON to stdout
ENTRYPOINT ["/app/entrypoint.sh"]
