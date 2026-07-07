#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${PYNCHY_DATA_DIR:-$REPO_ROOT/data}"
BACKUP_ROOT="${PYNCHY_BACKUP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/PynchyBackups}"
KEEP_DAYS="${PYNCHY_BACKUP_KEEP_DAYS:-30}"

if [[ -z "$BACKUP_ROOT" || "$BACKUP_ROOT" == "/" || "$BACKUP_ROOT" == "$HOME" ]]; then
  echo "Refusing unsafe backup root: $BACKUP_ROOT" >&2
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest_dir="$BACKUP_ROOT/$timestamp"
mkdir -p "$dest_dir"

dbs=(
  messages.db
  memories.db
  neonize.db
  temporal.db
)

for db in "${dbs[@]}"; do
  src="$DATA_DIR/$db"
  dest="$dest_dir/$db"
  if [[ -f "$src" ]]; then
    sqlite3 "$src" ".timeout 5000" ".backup '$dest'"
  fi
done

if command -v shasum >/dev/null 2>&1; then
  (
    cd "$dest_dir"
    shasum -a 256 ./*.db > SHA256SUMS
  )
fi

if ! find "$BACKUP_ROOT" \
  -mindepth 1 \
  -maxdepth 1 \
  -type d \
  -name '20*T*Z' \
  -mtime "+$KEEP_DAYS" \
  -exec rm -rf {} +; then
  echo "Warning: backup retention prune failed for $BACKUP_ROOT" >&2
fi

echo "Backed up runtime DBs to $dest_dir"
