#!/bin/sh
set -eu

display=${DISPLAY:-:0}
export DISPLAY="$display"

Xvfb "$display" -screen 0 1920x1080x24 -nolisten tcp &
xvfb_pid=$!
openbox &
openbox_pid=$!

# Invoked by trap.
# shellcheck disable=SC2329
cleanup() {
    kill "$openbox_pid" "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while kill -0 "$xvfb_pid" 2>/dev/null && kill -0 "$openbox_pid" 2>/dev/null; do
    sleep 5
done
exit 1
