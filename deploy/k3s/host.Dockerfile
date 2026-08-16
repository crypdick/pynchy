# Pynchy host and Kubernetes runtime adapter. Agent workloads use agent.Dockerfile.
FROM registry.k8s.io/kubectl:v1.36.3 AS kubectl

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
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
    novnc \
    procps \
    ripgrep \
    rsync \
    sqlite3 \
    x11vnc \
    xvfb \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && mkdir -p /usr/share/scrcpy \
    && curl -fsSL https://github.com/Genymobile/scrcpy/releases/download/v3.3.4/scrcpy-server-v3.3.4 -o /usr/share/scrcpy/scrcpy-server \
    && echo "8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e  /usr/share/scrcpy/scrcpy-server" | sha256sum -c - \
    && rm -rf /var/lib/apt/lists/*

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
    PYTHONUNBUFFERED=1
USER pynchy
WORKDIR /srv/pynchy/app

CMD ["pynchy"]
