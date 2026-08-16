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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/pynchy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache-dir '.[all]'

RUN groupadd --gid 3000 pynchy \
    && useradd --uid 3000 --gid 3000 --create-home --shell /bin/bash pynchy \
    && mkdir -p /srv/pynchy /run/pynchy \
    && chown -R pynchy:pynchy /srv/pynchy /run/pynchy

ENV HOME=/home/pynchy \
    PYTHONUNBUFFERED=1
USER pynchy
WORKDIR /srv/pynchy/app

CMD ["pynchy"]
