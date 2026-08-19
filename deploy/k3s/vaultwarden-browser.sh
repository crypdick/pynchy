#!/bin/sh
set -eu

profile=$1
shift
mkdir -p "$profile"
chmod 0700 "$profile"

display_file=$(mktemp)
config_file=$(mktemp)
trap 'rm -f "$display_file" "$config_file"' EXIT
printf '%s\n' '{"browser":{"launchOptions":{"ignoreDefaultArgs":["--disable-background-networking","--disable-component-update","--disable-extensions"]}}}' >"$config_file"

# NOTE: K3s blocks peer signals; pod teardown owns this pod-lifetime Xvfb process.
Xvfb -displayfd 3 -screen 0 1280x1024x24 -nolisten tcp 3>"$display_file" &
xvfb_pid=$!
tries=200
while [ ! -s "$display_file" ]; do
    kill -0 "$xvfb_pid" 2>/dev/null || {
        wait "$xvfb_pid"
        exit $?
    }
    [ "$tries" -gt 0 ] || {
        echo "Xvfb did not publish a display within 10 seconds" >&2
        exit 1
    }
    tries=$((tries - 1))
    sleep 0.05
done
display=$(cat "$display_file")
case "$display" in
    *[!0-9]* | "")
        echo "Xvfb published an invalid display" >&2
        exit 1
        ;;
esac

DISPLAY=":$display" playwright-mcp \
    --config "$config_file" \
    --executable-path /usr/bin/chromium \
    --no-sandbox \
    --shared-browser-context \
    --user-data-dir "$profile" \
    "$@"
