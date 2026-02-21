# Wraps the stdio-only mcp/gdrive with:
# 1. supergateway — SSE bridge for pynchy's HTTP-based MCP infrastructure
# 2. gdrive-wrapper.mjs — patches OAuth2 for automatic token refresh
# Build: docker build -t pynchy-mcp-gdrive -f container/mcp/gdrive.Dockerfile .
FROM mcp/gdrive:latest
RUN npm install -g supergateway
COPY container/mcp/gdrive-wrapper.mjs /app/gdrive-wrapper.mjs
ENTRYPOINT ["supergateway", "--stdio", "node /app/gdrive-wrapper.mjs", "--port", "3000"]
