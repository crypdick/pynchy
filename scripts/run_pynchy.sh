#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
pass_dir="$project_root/data/proton-pass"
host_config="$pass_dir/host.conf"
secret_template="$pass_dir/pynchy.env"
# Keep the live service outside the checkout's development environment so an
# operator's uv sync cannot prune production-only extras during an incident.
export UV_PROJECT_ENVIRONMENT="$project_root/data/host-venv"

if [ ! -f "$secret_template" ]; then
    exec uv run --locked --no-dev --all-extras pynchy
fi
if ! command -v pass-cli >/dev/null 2>&1; then
    echo "pynchy: data/proton-pass/pynchy.env requires pass-cli" >&2
    exit 1
fi
if [ -f "$host_config" ]; then
    set -a
    # host.conf contains Proton Pass session configuration, not provider secrets.
    . "$host_config"
    set +a
fi

exec pass-cli run --env-file "$secret_template" -- uv run --locked --no-dev --all-extras pynchy
