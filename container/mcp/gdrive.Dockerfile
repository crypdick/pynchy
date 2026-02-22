# Wraps the stdio-only mcp/gdrive with:
# 1. supergateway — Streamable HTTP bridge for pynchy's MCP infrastructure
# 2. gdrive-wrapper.mjs — patches OAuth2 for automatic token refresh
#
# Uses streamableHttp (not SSE) because supergateway's SSE mode creates a
# single Server instance that crashes on reconnection ("Already connected
# to a transport").  streamableHttp handles multiple sessions correctly.
#
# Build: docker build -t pynchy-mcp-gdrive -f container/mcp/gdrive.Dockerfile .
FROM mcp/gdrive:latest
RUN npm install -g supergateway
COPY container/mcp/gdrive-wrapper.mjs /app/gdrive-wrapper.mjs
ENV PORT=3100
ENTRYPOINT ["sh", "-c", "supergateway --stdio 'node /app/gdrive-wrapper.mjs' --outputTransport streamableHttp --port $PORT"]
