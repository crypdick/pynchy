#!/bin/sh
set -eu

# Match the production agent boundary: Pynchy mounts a per-workspace env
# directory, and the image entrypoint is responsible for loading it.
if [ -f /workspace/env-dir/env ]; then
  set -a
  . /workspace/env-dir/env
  set +a
fi

exec python -m agent_runner
