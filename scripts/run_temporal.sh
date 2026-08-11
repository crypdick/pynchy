#!/bin/sh
set -eu

temporal_bin=${1:?usage: run_temporal.sh TEMPORAL_BIN}
temporal_pid=

cleanup() {
    if [ -n "$temporal_pid" ] && kill -0 "$temporal_pid" 2>/dev/null; then
        kill -TERM "$temporal_pid"
        wait "$temporal_pid" || true
    fi
}

# launchd stops this launcher, so it must also stop the server it started.
trap cleanup EXIT
trap 'exit 0' HUP INT TERM

"$temporal_bin" server start-dev \
    --ip 127.0.0.1 \
    --port 7233 \
    --headless \
    --db-filename data/temporal.db \
    --log-level warn &
temporal_pid=$!

until "$temporal_bin" operator cluster health --address 127.0.0.1:7233 >/dev/null 2>&1; do
    if ! kill -0 "$temporal_pid" 2>/dev/null; then
        wait "$temporal_pid"
    fi
    sleep 1
done

"$temporal_bin" operator namespace update \
    --address 127.0.0.1:7233 \
    --namespace default \
    --retention 192h

wait "$temporal_pid"
