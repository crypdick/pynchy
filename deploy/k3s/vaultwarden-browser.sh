#!/bin/sh
set -eu

profile=$1
shift
mkdir -p "$profile"
chmod 0700 "$profile"

exec xvfb-run -a playwright-mcp \
    --executable-path /usr/bin/chromium \
    --no-sandbox \
    --shared-browser-context \
    --user-data-dir "$profile" \
    "$@"
