# Locked, minimal agent_runner image for the deterministic runtime harness.
# It deliberately excludes the mutable CLI/plugin tooling in Dockerfile while
# retaining the real OpenAI Agents SDK, streaming loop, and file IPC protocol.
FROM ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 AS uv

FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY agent_runner/pyproject.toml agent_runner/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY agent_runner/src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6
ENV VIRTUAL_ENV=/opt/pynchy/venv
ENV PATH="/opt/pynchy/venv/bin:${PATH}"
ENV PYTHONPATH=/opt/pynchy/agent-runner/src
ENV PYTHONUNBUFFERED=1
WORKDIR /opt/pynchy/agent-runner
COPY --from=build /app/.venv /opt/pynchy/venv
COPY agent_runner/src ./src
COPY runtime_entrypoint.sh /opt/pynchy/runtime-entrypoint
RUN chmod 0555 /opt/pynchy/runtime-entrypoint \
 && mkdir -p \
    /home/agent/src \
    /home/agent/workspace \
    /home/agent/skills \
    /home/agent/memory \
    /home/agent/automation-memory \
    /home/agent/mnt \
    /opt/pynchy/scripts \
    /opt/pynchy/plugin-hooks \
    /run/pynchy/messages /run/pynchy/requests /run/pynchy/input /run/pynchy/output \
    /tmp \
 && chmod 1777 /tmp

# Keep this harness image as root: Docker bind mounts inherit arbitrary local
# and CI host UIDs. Production uses its separate non-root agent image; this
# image exists to exercise the runner and IPC behavior without changing mount
# permissions solely for test infrastructure.
WORKDIR /home/agent/workspace
ENTRYPOINT ["/opt/pynchy/runtime-entrypoint"]
