#!/bin/bash
# Build the Pynchy agent container image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Detect container runtime (mirrors logic in src/pynchy/runtime.py)
if [ -n "$CONTAINER_RUNTIME" ]; then
    case "$CONTAINER_RUNTIME" in
        apple)  RUNTIME="container" ;;
        docker) RUNTIME="docker" ;;
        *)      echo "Unknown CONTAINER_RUNTIME: $CONTAINER_RUNTIME"; exit 1 ;;
    esac
elif [ "$(uname)" = "Darwin" ] && command -v container &>/dev/null; then
    RUNTIME="container"
elif command -v docker &>/dev/null; then
    RUNTIME="docker"
else
    echo "No container runtime found. Install Docker or Apple Container."
    exit 1
fi

IMAGE_NAME="pynchy-agent"
TAG="${1:-latest}"

echo "Building Pynchy agent container image..."
echo "Runtime: ${RUNTIME}"
echo "Image: ${IMAGE_NAME}:${TAG}"

# Generate container plugin requirements from currently installed plugins.
python3 ./scripts/generate_plugin_requirements.py --output ./requirements-plugins.txt

export DOCKER_BUILDKIT=1
$RUNTIME build -t "${IMAGE_NAME}:${TAG}" .

# Build MCP server images from mcp/*.Dockerfile in parallel.
# Image name derived from filename: notebook.Dockerfile → pynchy-mcp-notebook:latest
MCP_DIR="${SCRIPT_DIR}/mcp"
MCP_PIDS=()
if compgen -G "${MCP_DIR}/*.Dockerfile" > /dev/null 2>&1; then
    echo ""
    echo "Building MCP server images..."
    cd "${SCRIPT_DIR}/../../.."  # project root — Dockerfiles use paths relative to it
    for df in "${MCP_DIR}"/*.Dockerfile; do
        base="$(basename "$df" .Dockerfile)"
        mcp_image="pynchy-mcp-${base}:${TAG}"
        echo "  Building ${mcp_image} from ${df}"
        $RUNTIME build -t "${mcp_image}" -f "${df}" . &
        MCP_PIDS+=($!)
    done
    # Wait for all parallel MCP builds; fail the script if any fails.
    for pid in "${MCP_PIDS[@]}"; do
        wait "$pid" || { echo "MCP image build failed (pid $pid)"; exit 1; }
    done
    echo "All MCP images built."
fi

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"group_folder\":\"test\",\"chat_jid\":\"test@g.us\",\"is_admin\":false}' | $RUNTIME run -i ${IMAGE_NAME}:${TAG}"
