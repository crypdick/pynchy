#!/bin/sh
# Install the standalone Codex CLI package into a location readable by the
# non-root agent user.

set -eu

release="${CODEX_RELEASE:-latest}"
codex_home="${CODEX_HOME:-/opt/codex}"
install_dir="${CODEX_INSTALL_DIR:-/usr/local/bin}"

case "$(uname -m)" in
    aarch64|arm64)
        target="aarch64-unknown-linux-musl"
        ;;
    x86_64|amd64)
        target="x86_64-unknown-linux-musl"
        ;;
    *)
        echo "Unsupported Codex architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

case "$release" in
    ""|latest)
        metadata_url="https://api.github.com/repos/openai/codex/releases/latest"
        ;;
    rust-v*)
        metadata_url="https://api.github.com/repos/openai/codex/releases/tags/$release"
        ;;
    v*)
        metadata_url="https://api.github.com/repos/openai/codex/releases/tags/rust-${release}"
        ;;
    *)
        metadata_url="https://api.github.com/repos/openai/codex/releases/tags/rust-v${release}"
        ;;
esac

release_json="$(curl -fsSL "$metadata_url")"
version="$(printf '%s' "$release_json" | jq -er '.tag_name | sub("^rust-v"; "")')"
asset="codex-package-${target}.tar.gz"
download_url="$(
    printf '%s' "$release_json" \
        | jq -er --arg name "$asset" '.assets[] | select(.name == $name) | .browser_download_url'
)"
expected_sha="$(
    printf '%s' "$release_json" \
        | jq -er --arg name "$asset" '.assets[] | select(.name == $name) | .digest | sub("^sha256:"; "")'
)"

tmp_dir="$(mktemp -d)"
cleanup() {
    rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

archive_path="$tmp_dir/$asset"
curl -fsSL "$download_url" -o "$archive_path"
actual_sha="$(sha256sum "$archive_path" | awk '{print $1}')"
if [ "$actual_sha" != "$expected_sha" ]; then
    echo "Codex archive checksum mismatch for $asset" >&2
    echo "expected: $expected_sha" >&2
    echo "actual:   $actual_sha" >&2
    exit 1
fi

standalone_root="$codex_home/packages/standalone"
release_dir="$standalone_root/releases/${version}-${target}"
rm -rf "$release_dir"
mkdir -p "$release_dir" "$install_dir"
tar -xzf "$archive_path" -C "$release_dir"

chmod 0755 \
    "$release_dir/bin/codex" \
    "$release_dir/bin/codex-code-mode-host" \
    "$release_dir/codex-path/rg" \
    "$release_dir/codex-resources/bwrap"

ln -sf "bin/codex" "$release_dir/codex"
ln -sfn "$release_dir" "$standalone_root/current"
ln -sfn "$standalone_root/current/bin/codex" "$install_dir/codex"
chmod -R a+rX "$codex_home"

"$install_dir/codex" --version
