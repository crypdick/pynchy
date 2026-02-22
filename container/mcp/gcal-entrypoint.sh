#!/bin/sh
# Entrypoint for pynchy-mcp-gcal container.
#
# Converts pynchy's flat credentials.json → gcal's token path.
# gcal (@cocal/google-calendar-mcp) auto-migrates single-account →
# multi-account format on first load, so we just copy our flat tokens.
#
# Chrome profile directory is mounted at /home/chrome/ by the MCP manager.
# Contains:
#   gcp-oauth.keys.json  — OAuth client credentials (from setup)
#   credentials.json     — access/refresh tokens (from setup)

if [ -f /home/chrome/credentials.json ]; then
  mkdir -p /home/chrome/.gcal
  cp /home/chrome/credentials.json /home/chrome/.gcal/tokens.json
fi

export GOOGLE_OAUTH_CREDENTIALS=/home/chrome/gcp-oauth.keys.json
export GOOGLE_CALENDAR_MCP_TOKEN_PATH=/home/chrome/.gcal/tokens.json

# PORT is set by Docker env from plugin-assigned port
exec google-calendar-mcp --transport http --port "${PORT:-3200}"
