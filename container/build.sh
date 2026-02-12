#!/bin/bash
# Build the Pynchy agent container image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="pynchy-agent"
TAG="${1:-latest}"

echo "Building Pynchy agent container image..."
echo "Image: ${IMAGE_NAME}:${TAG}"

# Build with Apple Container
container build -t "${IMAGE_NAME}:${TAG}" .

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"group_folder\":\"test\",\"chat_jid\":\"test@g.us\",\"is_main\":false}' | container run -i ${IMAGE_NAME}:${TAG}"
