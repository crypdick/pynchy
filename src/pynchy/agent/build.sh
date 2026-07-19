#!/bin/bash
# Build the Pynchy agent container image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
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

cleanup_runtime_build_state() {
    if [ "$RUNTIME" = "container" ] && [ "${PYNCHY_KEEP_APPLE_BUILDER:-}" != "1" ]; then
        echo "Cleaning Apple Container builder..."
        $RUNTIME builder stop >/dev/null 2>&1 || true
        $RUNTIME builder rm --force >/dev/null 2>&1 || true
    fi

    if [ "${PYNCHY_PRUNE_IMAGES:-1}" != "0" ]; then
        echo "Pruning dangling container images..."
        if [ "$RUNTIME" = "docker" ]; then
            $RUNTIME image prune -f >/dev/null 2>&1 || true
        else
            $RUNTIME image prune >/dev/null 2>&1 || true
        fi
    fi
}
trap cleanup_runtime_build_state EXIT

IMAGE_NAME="pynchy-agent"
TAG="${1:-latest}"

echo "Building Pynchy agent container image..."
echo "Runtime: ${RUNTIME}"
echo "Image: ${IMAGE_NAME}:${TAG}"

# Generate container plugin requirements from currently installed plugins.
uv run python ./scripts/generate_plugin_requirements.py \
    --output ./requirements-plugins.txt \
    --config "${PROJECT_ROOT}/config.toml"

export DOCKER_BUILDKIT=1
$RUNTIME build -t "${IMAGE_NAME}:${TAG}" .

# Build MCP server images from mcp/*.Dockerfile.
# Image name derived from filename: notebook.Dockerfile → pynchy-mcp-notebook:latest
MCP_DIR="${SCRIPT_DIR}/mcp"
MCP_PIDS=()
if compgen -G "${MCP_DIR}/*.Dockerfile" > /dev/null 2>&1; then
    echo ""
    echo "Building MCP server images..."
    # Apple Container shares one BuildKit builder between CLI clients. Concurrent
    # builds can leave that builder stuck or report a failed aggregate build, so
    # serialize them there. Docker keeps the faster parallel path.
    if [ "$RUNTIME" = "container" ]; then
        echo "  Serializing builds for Apple Container"
    fi
    cd "${SCRIPT_DIR}/../../.."  # project root — Dockerfiles use paths relative to it
    MCP_FAILED=0
    for df in "${MCP_DIR}"/*.Dockerfile; do
        base="$(basename "$df" .Dockerfile)"
        mcp_image="pynchy-mcp-${base}:${TAG}"
        echo "  Building ${mcp_image} from ${df}"
        if [ "$RUNTIME" = "container" ]; then
            if ! $RUNTIME build -t "${mcp_image}" -f "${df}" .; then
                echo "MCP image build failed: ${mcp_image}"
                MCP_FAILED=1
            fi
        else
            $RUNTIME build -t "${mcp_image}" -f "${df}" . &
            MCP_PIDS+=($!)
        fi
    done
    if [ "$RUNTIME" != "container" ]; then
        # Wait for all parallel Docker builds; fail the script if any fails.
        for pid in "${MCP_PIDS[@]}"; do
            if ! wait "$pid"; then
                echo "MCP image build failed (pid $pid)"
                MCP_FAILED=1
            fi
        done
    fi
    if [ "$MCP_FAILED" -ne 0 ]; then
        exit 1
    fi
    echo "All MCP images built."
fi

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"group_folder\":\"test\",\"chat_jid\":\"test@g.us\",\"is_admin\":false}' | $RUNTIME run -i ${IMAGE_NAME}:${TAG}"
