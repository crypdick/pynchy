#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporal_bin=${1:?usage: run_temporal.sh TEMPORAL_BIN}
address=127.0.0.1:7233

"$temporal_bin" server start-dev \
    --ip 127.0.0.1 \
    --port 7233 \
    --headless \
    --db-filename "$project_root/data/temporal.db" \
    --log-level warn &
temporal_pid=$!

stop_temporal() {
    trap - EXIT INT TERM
    kill "$temporal_pid" 2>/dev/null || true
    wait "$temporal_pid" 2>/dev/null || true
}
trap 'stop_temporal; exit 130' INT
trap 'stop_temporal; exit 143' TERM
trap stop_temporal EXIT

until "$temporal_bin" --address "$address" operator cluster health >/dev/null 2>&1; do
    if ! kill -0 "$temporal_pid" 2>/dev/null; then
        wait "$temporal_pid"
        exit $?
    fi
    sleep 1
done

# Temporal owns scheduler history, so enforce retention at its local service boundary.
"$temporal_bin" --address "$address" operator namespace update \
    --namespace default \
    --retention 192h

wait "$temporal_pid"
