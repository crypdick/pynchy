# Google Calendar MCP server (@cocal/google-calendar-mcp).
# Native Streamable HTTP — no supergateway needed.
# Credentials are mounted from the chrome profile directory at /home/chrome/.
#
# Build: docker build -t pynchy-mcp-gcal -f src/pynchy/agent/mcp/gcal.Dockerfile .
FROM node:22-slim
RUN npm install -g @cocal/google-calendar-mcp@latest
COPY src/pynchy/agent/mcp/gcal-entrypoint.sh /app/gcal-entrypoint.sh
RUN chmod +x /app/gcal-entrypoint.sh
ENV PORT=3200
ENTRYPOINT ["/app/gcal-entrypoint.sh"]
