# Wraps the stdio-only mcp/gdrive with supergateway to expose SSE over HTTP.
# Build: docker build -t pynchy-mcp-gdrive -f container/mcp/gdrive.Dockerfile .
FROM mcp/gdrive:latest
RUN npm install -g supergateway
ENTRYPOINT ["supergateway", "--stdio", "node dist/index.js", "--port", "3000"]
