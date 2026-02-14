# Pynchy Agent Container
# Runs Claude Agent SDK (Python) in isolated Linux VM with browser automation

FROM python:3.13-slim

# Install uv (available to all containers for running Python tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install system dependencies for Chromium + common tools
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    fonts-noto-color-emoji \
    libgbm1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    curl \
    git \
    ripgrep \
    jq \
    procps \
    gpg \
    libmagic1 \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y gh nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium path for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Disable Python stdout buffering so streaming markers reach the host immediately
ENV PYTHONUNBUFFERED=1

# Install agent-browser and claude-code globally
RUN npm install -g agent-browser @anthropic-ai/claude-code

# Create app directory
WORKDIR /app

# Copy Python agent runner and install
COPY agent_runner/ /app/
RUN pip install --no-cache-dir /app/

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create entrypoint script
# Sources env from mounted /workspace/env-dir/env if it exists (set -a exports all vars)
# Stdin is buffered to /tmp then piped (Apple Container requires EOF to flush stdin pipe)
RUN printf '#!/bin/bash\nset -e\nif [ -f /workspace/env-dir/env ]; then set -a; . /workspace/env-dir/env; set +a; fi\ncat > /tmp/input.json\npython -m agent_runner < /tmp/input.json\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Create non-root user for security
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
