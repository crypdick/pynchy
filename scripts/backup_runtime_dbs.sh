#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${PYNCHY_DATA_DIR:-$REPO_ROOT/data}"
BACKUP_ROOT="${PYNCHY_BACKUP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/PynchyBackups}"
KEEP_DAYS="${PYNCHY_BACKUP_KEEP_DAYS:-30}"
TEMPORAL_LABEL="${PYNCHY_TEMPORAL_LABEL:-com.pynchy.temporal}"
TEMPORAL_PLIST="${PYNCHY_TEMPORAL_PLIST:-$HOME/Library/LaunchAgents/$TEMPORAL_LABEL.plist}"

if [[ -z "$BACKUP_ROOT" || "$BACKUP_ROOT" == "/" || "$BACKUP_ROOT" == "$HOME" ]]; then
  echo "Refusing unsafe backup root: $BACKUP_ROOT" >&2
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest_dir="$BACKUP_ROOT/$timestamp"
staging_dir="$BACKUP_ROOT/.partial-$timestamp-$$"
launchd_domain="gui/$(id -u)"
temporal_restart_pending=false

resume_temporal() {
  if [[ "$temporal_restart_pending" != true ]]; then
    return 0
  fi
  if [[ ! -f "$TEMPORAL_PLIST" ]]; then
    echo "Cannot restart Temporal; plist does not exist: $TEMPORAL_PLIST" >&2
    return 1
  fi
  launchctl bootstrap "$launchd_domain" "$TEMPORAL_PLIST"
  temporal_restart_pending=false
}

finish_after_error() {
  status=$?
  trap - EXIT
  if ! resume_temporal; then
    status=1
  fi
  if [[ -d "$staging_dir" ]]; then
    echo "Incomplete backup retained at $staging_dir" >&2
  fi
  exit "$status"
}

quiesce_temporal() {
  temporal_db=$1
  service_target="$launchd_domain/$TEMPORAL_LABEL"

  # Temporal's start-dev SQLite backend can remain wedged after an online
  # backup collides with a write. Stop the managed service before opening its
  # database, then let the EXIT trap restore it if any later command fails.
  if command -v launchctl >/dev/null 2>&1 \
    && launchctl print "$service_target" >/dev/null 2>&1; then
    temporal_restart_pending=true
    launchctl bootout "$service_target"
  fi

  if ! command -v lsof >/dev/null 2>&1; then
    if [[ "$temporal_restart_pending" != true ]]; then
      echo "Cannot prove Temporal is quiescent: neither a managed service nor lsof is available" >&2
      return 1
    fi
    return 0
  fi

  for _attempt in {1..50}; do
    if ! lsof "$temporal_db" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "Refusing to back up Temporal while its database remains open: $temporal_db" >&2
  return 1
}

mkdir -p "$BACKUP_ROOT"
mkdir "$staging_dir"
trap finish_after_error EXIT

dbs=(
  messages.db
  memories.db
  neonize.db
)

for db in "${dbs[@]}"; do
  src="$DATA_DIR/$db"
  dest="$staging_dir/$db"
  if [[ -f "$src" ]]; then
    sqlite3 "$src" ".timeout 5000" ".backup '$dest'"
  fi
done

temporal_src="$DATA_DIR/temporal.db"
if [[ -f "$temporal_src" ]]; then
  quiesce_temporal "$temporal_src"
  sqlite3 "$temporal_src" ".timeout 5000" ".backup '$staging_dir/temporal.db'"
  resume_temporal
fi

if command -v shasum >/dev/null 2>&1; then
  (
    cd "$staging_dir"
    shasum -a 256 ./*.db > SHA256SUMS
  )
fi

mv "$staging_dir" "$dest_dir"
trap - EXIT

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
