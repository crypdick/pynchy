#!/bin/sh
set -eu

display=${DISPLAY:-:0}
export DISPLAY="$display"

profile=${PYNCHY_CHROMIUM_PROFILE:-/home/pynchy/.config/chromium}
for name in SingletonLock SingletonSocket SingletonCookie; do
    rm -f "$profile/$name"
done

Xvfb "$display" -screen 0 1920x1080x24 -nolisten tcp &
xvfb_pid=$!
while ! xdotool getmouselocation >/dev/null 2>&1; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        wait "$xvfb_pid" || true
        exit 1
    fi
    sleep 1
done
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
