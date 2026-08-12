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

# Apple Container has one BuildKit builder per host user, shared by cron,
# deploys, and worktrees. Keep prune/build/EXIT cleanup in one ownership span.
if [ "$RUNTIME" = "container" ] && [ "${PYNCHY_APPLE_BUILD_LOCK_HELD:-}" != "1" ]; then
    exec uv run python -m pynchy.plugins.runtimes.apple_build_lock --exec "$0" "$@"
fi

cleanup_runtime_build_state() {
    local cleanup_failed=0

    if [ "${PYNCHY_PRUNE_IMAGES:-1}" != "0" ]; then
        echo "Pruning dangling container images..."
        if [ "$RUNTIME" = "docker" ]; then
            if ! $RUNTIME image prune -f; then
                cleanup_failed=1
            fi
        else
            if ! $RUNTIME image prune; then
                cleanup_failed=1
            fi
        fi
    fi

    return "$cleanup_failed"
}

cleanup_failed_apple_builder() {
    if [ "$RUNTIME" = "container" ]; then
        echo "Cleaning failed Apple Container builder..."
        $RUNTIME builder stop || true
        $RUNTIME builder rm --force || true
    fi
}

cleanup_runtime_build_state_on_exit() {
    local build_status=$?
    local cleanup_status=0
    if [ "$build_status" -ne 0 ]; then
        cleanup_failed_apple_builder
    fi
    if ! cleanup_runtime_build_state; then
        echo "Container build-state cleanup failed." >&2
        cleanup_status=1
    fi
    if [ "$build_status" -ne 0 ]; then
        exit "$build_status"
    fi
    exit "$cleanup_status"
}

terminate_build() {
    exit 143
}

trap terminate_build TERM INT
trap cleanup_runtime_build_state_on_exit EXIT

if ! cleanup_runtime_build_state; then
    echo "Refusing to build with stale container build state." >&2
    exit 1
fi

mcp_image_fingerprint() {
    local base="$1"
    local pathspecs=()
    case "$base" in
        gcal)
            pathspecs=(
                "src/pynchy/agent/mcp/gcal.Dockerfile"
                "src/pynchy/agent/mcp/gcal-entrypoint.sh"
            )
            ;;
        gdrive)
            pathspecs=(
                "src/pynchy/agent/mcp/gdrive.Dockerfile"
                "src/pynchy/agent/mcp/gdrive-wrapper.mjs"
            )
            ;;
        notebook)
            pathspecs=(
                "src/pynchy/agent/mcp/notebook.Dockerfile"
                "src/pynchy/plugins/integrations/notebook_server"
            )
            ;;
        *)
            return 1
            ;;
    esac

    local tracked_files
    tracked_files="$(
        git -C "$PROJECT_ROOT" ls-files --cached --others --exclude-standard -- "${pathspecs[@]}"
    )"
    if [ -z "$tracked_files" ]; then
        return 1
    fi

    while IFS= read -r tracked_file; do
        printf '%s\t%s\n' \
            "$tracked_file" \
            "$(git -C "$PROJECT_ROOT" hash-object "$PROJECT_ROOT/$tracked_file")"
    done <<< "$tracked_files" | git hash-object --stdin
}

mcp_image_is_current() {
    local base="$1"
    local image="$2"
    local fingerprint="$3"
    local stamp="$MCP_STAMP_DIR/${base}-${TAG//\//_}.fingerprint"
    local recorded_fingerprint=""

    if [ "${PYNCHY_REBUILD_MCP:-0}" = "1" ] || [ ! -f "$stamp" ]; then
        return 1
    fi
    IFS= read -r recorded_fingerprint < "$stamp"
    [ "$recorded_fingerprint" = "$fingerprint" ] && \
        "$RUNTIME" image inspect "$image" >/dev/null 2>&1
}

record_mcp_image_fingerprint() {
    local base="$1"
    local fingerprint="$2"
    local stamp="$MCP_STAMP_DIR/${base}-${TAG//\//_}.fingerprint"
    local temporary_stamp="${stamp}.tmp.$$"

    mkdir -p "$MCP_STAMP_DIR"
    printf '%s\n' "$fingerprint" > "$temporary_stamp"
    mv "$temporary_stamp" "$stamp"
}

IMAGE_NAME="pynchy-agent"
TAG="${1:-latest}"

echo "Building Pynchy agent container image..."
echo "Runtime: ${RUNTIME}"
echo "Image: ${IMAGE_NAME}:${TAG}"

# Generate container plugin requirements from currently installed plugins.
uv run python ./scripts/generate_plugin_requirements.py \
    --output ./requirements-plugins.txt \
    --config "${PROJECT_ROOT}/data/personalization/pynchy.toml"

export DOCKER_BUILDKIT=1
$RUNTIME build -t "${IMAGE_NAME}:${TAG}" .

# Build MCP server images from mcp/*.Dockerfile.
# Image name derived from filename: notebook.Dockerfile → pynchy-mcp-notebook:latest
MCP_DIR="${SCRIPT_DIR}/mcp"
MCP_STAMP_DIR="${PROJECT_ROOT}/data/build-cache/mcp-images"
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
    MCP_FAILED=0
    for df in "${MCP_DIR}"/*.Dockerfile; do
        base="$(basename "$df" .Dockerfile)"
        mcp_image="pynchy-mcp-${base}:${TAG}"
        mcp_fingerprint=""
        mcp_context="$MCP_DIR"
        if [ "$base" = "notebook" ]; then
            mcp_context="$PROJECT_ROOT/src/pynchy/plugins/integrations"
        fi
        if [ "$RUNTIME" = "container" ]; then
            # Normal deploys reuse content-identical MCP images. Scheduled
            # maintenance sets PYNCHY_REBUILD_MCP=1 to refresh floating bases.
            if mcp_fingerprint="$(mcp_image_fingerprint "$base")" && \
                mcp_image_is_current "$base" "$mcp_image" "$mcp_fingerprint"; then
                echo "  Reusing ${mcp_image}; source inputs are unchanged"
                continue
            fi
        fi
        echo "  Building ${mcp_image} from ${df}"
        if [ "$RUNTIME" = "container" ]; then
            if ! $RUNTIME build -t "${mcp_image}" -f "${df}" "$mcp_context"; then
                echo "MCP image build failed: ${mcp_image}"
                MCP_FAILED=1
            else
                if [ -n "$mcp_fingerprint" ]; then
                    record_mcp_image_fingerprint "$base" "$mcp_fingerprint"
                fi
            fi
        else
            $RUNTIME build -t "${mcp_image}" -f "${df}" "$mcp_context" &
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
