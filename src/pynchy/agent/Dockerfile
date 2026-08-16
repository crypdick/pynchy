# Pynchy Agent Container
# Runs agent cores (Claude SDK, OpenAI Agents SDK, Codex CLI) in isolated Linux VM

FROM python:3.13-slim

# Install uv (available to all containers for running Python tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install system dependencies, common tools, and the native build toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ripgrep \
    jq \
    procps \
    rsync \
    sqlite3 \
    sudo \
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

# Disable Python stdout buffering so stderr log lines reach the host immediately
ENV PYTHONUNBUFFERED=1

# Install CLI agent backends globally
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force
COPY install_codex.sh /tmp/install_codex.sh
RUN /bin/sh /tmp/install_codex.sh && rm /tmp/install_codex.sh

# Install Claude Code plugins (loaded via Agent SDK's plugins option at runtime)
RUN git clone --depth 1 https://github.com/obra/superpowers.git /opt/plugins/superpowers

# Create harness directory
WORKDIR /opt/pynchy/agent-runner

# Copy Python agent runner and install with uv
COPY agent_runner/ /opt/pynchy/agent-runner/
RUN uv pip install --system --no-cache-dir /opt/pynchy/agent-runner

# Pynchy uses prek for repository hooks.
RUN uv pip install --system --no-cache-dir prek

# Install custom plugin dependencies (users add packages to this file)
COPY requirements-plugins.txt /opt/pynchy/requirements-plugins.txt
RUN uv pip install --system --no-cache-dir -r /opt/pynchy/requirements-plugins.txt

# Create agent and harness directories. /tmp is intentionally empty in the image
# and remains a normal sticky temporary directory at runtime.
RUN mkdir -p \
    /home/agent/src \
    /home/agent/workspace \
    /home/agent/skills \
    /home/agent/memory \
    /home/agent/automation-memory \
    /home/agent/mnt \
    /opt/pynchy/scripts \
    /opt/pynchy/agent-runner/src \
    /opt/pynchy/plugin-hooks \
    /run/pynchy/messages /run/pynchy/requests /run/pynchy/input /run/pynchy/output \
    /tmp \
 && find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + \
 && chmod 1777 /tmp

# Copy entrypoint script (restores .claude.json from backup, runs agent)
COPY entrypoint.sh /opt/pynchy/entrypoint.sh
RUN chmod +x /opt/pynchy/entrypoint.sh

# Create non-root user with passwordless sudo. Kubernetes builds use UID/GID
# 3000 so host and agent can safely share imported session state.
# Claude Code refuses --dangerously-skip-permissions as root, so we run as a
# regular user but grant sudo so agents can apt-get install tools they need.
ARG AGENT_UID=1000
ARG AGENT_GID=1000
RUN groupadd --gid "$AGENT_GID" agent \
 && useradd --uid "$AGENT_UID" --gid "$AGENT_GID" -m -s /bin/bash agent \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && chown -R agent:agent /home/agent /opt/pynchy /run/pynchy

USER agent

# Delegate git authentication to gh CLI (uses GH_TOKEN from environment at runtime)
# Rewrite SSH remote URLs to HTTPS so the credential helper works (no SSH keys in container)
RUN git config --global credential.helper '!gh auth git-credential' \
 && git config --global url."https://github.com/".insteadOf "git@github.com:"

# Set working directory to group workspace
WORKDIR /home/agent/workspace

# Entry point reads initial input from IPC files, writes output as IPC files
ENTRYPOINT ["/opt/pynchy/entrypoint.sh"]
