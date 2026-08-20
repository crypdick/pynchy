# Pynchy host and Kubernetes runtime adapter. Agent workloads use agent.Dockerfile.
FROM registry.k8s.io/kubectl:v1.36.3 AS kubectl

FROM python:3.13-slim

ARG PYNCHY_RELEASE_SHA
LABEL org.opencontainers.image.revision=${PYNCHY_RELEASE_SHA}

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl

RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    ca-certificates \
    chromium \
    curl \
    ffmpeg \
    gh \
    git \
    jq \
    imagemagick \
    novnc \
    openbox \
    openssh-client \
    procps \
    ripgrep \
    rsync \
    sqlite3 \
    unzip \
    wmctrl \
    xauth \
    xdotool \
    x11vnc \
    xvfb \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @playwright/mcp@0.0.79 \
    && mkdir -p /usr/share/scrcpy \
    && curl -fsSL https://github.com/Genymobile/scrcpy/releases/download/v3.3.4/scrcpy-server-v3.3.4 -o /usr/share/scrcpy/scrcpy-server \
    && echo "8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e  /usr/share/scrcpy/scrcpy-server" | sha256sum -c - \
    && curl -fsSL https://github.com/bitwarden/clients/releases/download/cli-v2026.7.0/bw-linux-2026.7.0.zip -o /tmp/bw.zip \
    && echo "7a35145e205952f7434d2370da359543145ae0c45ba1af0fe9bdd99d40a00180  /tmp/bw.zip" | sha256sum -c - \
    && unzip /tmp/bw.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/bw \
    && rm /tmp/bw.zip \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 deploy/k3s/desktop-entrypoint.sh /usr/local/bin/pynchy-desktop
COPY --chmod=755 deploy/k3s/vaultwarden-browser.sh /usr/local/bin/pynchy-vaultwarden-browser

COPY src/pynchy/agent/install_codex.sh /tmp/install_codex.sh
RUN /bin/sh /tmp/install_codex.sh && rm /tmp/install_codex.sh

WORKDIR /opt/pynchy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --all-extras --no-editable --no-cache

RUN groupadd --gid 3000 pynchy \
    && useradd --uid 3000 --gid 3000 --create-home --shell /bin/bash pynchy \
    && mkdir -p /srv/pynchy /run/pynchy \
    && chown -R pynchy:pynchy /srv/pynchy /run/pynchy

ENV HOME=/home/pynchy \
    PATH=/opt/pynchy/.venv/bin:$PATH \
    PYNCHY_RELEASE_SHA=${PYNCHY_RELEASE_SHA} \
    PYTHONUNBUFFERED=1
USER pynchy
WORKDIR /srv/pynchy/app

CMD ["pynchy"]
