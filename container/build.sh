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

$RUNTIME build -t "${IMAGE_NAME}:${TAG}" .

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"group_folder\":\"test\",\"chat_jid\":\"test@g.us\",\"is_main\":false}' | $RUNTIME run -i ${IMAGE_NAME}:${TAG}"
