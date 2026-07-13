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
RUN uv sync --locked --no-dev

FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY agent_runner/src ./src
COPY runtime_entrypoint.sh /usr/local/bin/pynchy-runtime-entrypoint
RUN chmod 0555 /usr/local/bin/pynchy-runtime-entrypoint

# Keep this harness image as root: Docker bind mounts inherit arbitrary local
# and CI host UIDs. Production uses its separate non-root agent image; this
# image exists to exercise the runner and IPC behavior without changing mount
# permissions solely for test infrastructure.
WORKDIR /workspace/group
ENTRYPOINT ["/usr/local/bin/pynchy-runtime-entrypoint"]
