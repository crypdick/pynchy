#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${PYNCHY_DATA_DIR:-$REPO_ROOT/data}"
BACKUP_ROOT="${PYNCHY_BACKUP_DIR:-$REPO_ROOT/data/backups}"
REMOTE_HOST="${PYNCHY_BACKUP_REMOTE_HOST:-}"
REMOTE_ROOT="${PYNCHY_BACKUP_REMOTE_DIR:-}"
SSH_KEY="${PYNCHY_BACKUP_SSH_KEY:-}"
STAGING_ROOT="${PYNCHY_BACKUP_STAGING_DIR:-$REPO_ROOT/data/backup-staging}"
KEEP_DAYS="${PYNCHY_BACKUP_KEEP_DAYS:-30}"
KEEP_COUNT="${PYNCHY_BACKUP_KEEP_COUNT:-0}"
TEMPORAL_LABEL="${PYNCHY_TEMPORAL_LABEL:-com.pynchy.temporal}"
TEMPORAL_PLIST="${PYNCHY_TEMPORAL_PLIST:-$HOME/Library/LaunchAgents/$TEMPORAL_LABEL.plist}"

if [[ -n "$REMOTE_HOST" && -z "$REMOTE_ROOT" ]] \
  || [[ -z "$REMOTE_HOST" && -n "$REMOTE_ROOT" ]]; then
  echo "PYNCHY_BACKUP_REMOTE_HOST and PYNCHY_BACKUP_REMOTE_DIR must be set together" >&2
  exit 2
fi

if [[ ! "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  echo "PYNCHY_BACKUP_KEEP_DAYS must be a non-negative integer" >&2
  exit 2
fi
if [[ ! "$KEEP_COUNT" =~ ^[0-9]+$ ]]; then
  echo "PYNCHY_BACKUP_KEEP_COUNT must be a non-negative integer" >&2
  exit 2
fi

remote_mode=false
if [[ -n "$REMOTE_HOST" ]]; then
  remote_mode=true
  if [[ ! "$REMOTE_HOST" =~ ^[A-Za-z0-9_.:@-]+$ ]]; then
    echo "PYNCHY_BACKUP_REMOTE_HOST contains unsupported characters" >&2
    exit 2
  fi
  if [[ ! "$REMOTE_ROOT" =~ ^/[A-Za-z0-9_./-]+$ ]] \
    || [[ "$REMOTE_ROOT" == "/" ]] \
    || [[ "$REMOTE_ROOT" == *"/../"* ]] \
    || [[ "$REMOTE_ROOT" == */.. ]] \
    || [[ "$REMOTE_ROOT" == *"/./"* ]] \
    || [[ "$REMOTE_ROOT" == */. ]]; then
    echo "PYNCHY_BACKUP_REMOTE_DIR must be a safe absolute path below /" >&2
    exit 2
  fi
  if [[ -n "$SSH_KEY" && ! -f "$SSH_KEY" ]]; then
    echo "PYNCHY_BACKUP_SSH_KEY does not exist: $SSH_KEY" >&2
    exit 2
  fi
fi

if [[ -z "$BACKUP_ROOT" || "$BACKUP_ROOT" == "/" || "$BACKUP_ROOT" == "$HOME" ]]; then
  echo "Refusing unsafe backup root: $BACKUP_ROOT" >&2
  exit 2
fi
if [[ "$remote_mode" == true ]] \
  && [[ -z "$STAGING_ROOT" || "$STAGING_ROOT" == "/" || "$STAGING_ROOT" == "$HOME" ]]; then
  echo "Refusing unsafe backup staging root: $STAGING_ROOT" >&2
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest_dir="$BACKUP_ROOT/$timestamp"
if [[ "$remote_mode" == true ]]; then
  staging_dir="$STAGING_ROOT/.partial-$timestamp-$$"
else
  staging_dir="$BACKUP_ROOT/.partial-$timestamp-$$"
fi
launchd_domain="gui/$(id -u)"
temporal_restart_pending=false
remote_partial=""

ssh_command=(ssh -o BatchMode=yes)
if [[ -n "$SSH_KEY" ]]; then
  ssh_command+=(-o IdentitiesOnly=yes -o IdentityAgent=none -i "$SSH_KEY")
fi

run_remote() {
  "${ssh_command[@]}" "$REMOTE_HOST" "$1"
}

remove_snapshot_tree() {
  root=$1
  snapshot=$2
  name=${snapshot##*/}
  parent=${snapshot%/*}

  if [[ "$parent" != "$root" || ! "$name" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]]; then
    echo "Refusing unsafe snapshot removal: $snapshot" >&2
    return 2
  fi

  find "$snapshot" -type f -exec rm {} \;
  find "$snapshot" -type l -exec rm {} \;
  find "$snapshot" -depth -type d -exec rmdir {} \;
}

prune_snapshot_root() {
  root=$1
  keep_days=$2
  keep_count=$3

  while IFS= read -r snapshot; do
    remove_snapshot_tree "$root" "$snapshot"
  done < <(
    find "$root" \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -name '20*T*Z' \
      -mtime "+$keep_days" \
      -print \
      | sort
  )

  if [[ "$keep_count" == 0 ]]; then
    return 0
  fi

  retained=0
  while IFS= read -r snapshot; do
    retained=$((retained + 1))
    if ((retained > keep_count)); then
      remove_snapshot_tree "$root" "$snapshot"
    fi
  done < <(
    find "$root" \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -name '20*T*Z' \
      -print \
      | sort -r
  )
}

prune_remote_backups() {
  {
    declare -f remove_snapshot_tree
    declare -f prune_snapshot_root
    # shellcheck disable=SC2016  # Expand positional arguments in the remote shell.
    echo 'prune_snapshot_root "$1" "$2" "$3"'
  } | "${ssh_command[@]}" "$REMOTE_HOST" \
    bash -s -- "$REMOTE_ROOT" "$KEEP_DAYS" "$KEEP_COUNT"
}

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
  if [[ -n "$remote_partial" ]]; then
    echo "Remote partial backup may remain at $REMOTE_HOST:$remote_partial" >&2
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

if [[ "$remote_mode" == true ]]; then
  mkdir -p "$STAGING_ROOT"
else
  mkdir -p "$BACKUP_ROOT"
fi
mkdir "$staging_dir"
trap finish_after_error EXIT

dbs=(
  messages.db
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
elif command -v sha256sum >/dev/null 2>&1; then
  (
    cd "$staging_dir"
    sha256sum ./*.db > SHA256SUMS
  )
else
  echo "Cannot create backup manifest: shasum or sha256sum is required" >&2
  exit 1
fi

if [[ "$remote_mode" == true ]]; then
  remote_partial="$REMOTE_ROOT/.partial-$timestamp-$$"
  remote_dest="$REMOTE_ROOT/$timestamp"
  run_remote "mkdir -p $REMOTE_ROOT && test ! -e $remote_partial && test ! -e $remote_dest && mkdir $remote_partial"

  rsync_shell="ssh -o BatchMode=yes"
  if [[ -n "$SSH_KEY" ]]; then
    printf -v quoted_ssh_key '%q' "$SSH_KEY"
    rsync_shell+=" -o IdentitiesOnly=yes -o IdentityAgent=none -i $quoted_ssh_key"
  fi
  rsync -a -e "$rsync_shell" -- "$staging_dir/" "$REMOTE_HOST:$remote_partial/"
  run_remote "cd $remote_partial && sha256sum -c SHA256SUMS && cd $REMOTE_ROOT && mv .partial-$timestamp-$$ $timestamp"
  remote_partial=""
  rm -rf -- "$staging_dir"
  trap - EXIT

  if ! prune_remote_backups; then
    echo "Warning: remote backup retention prune failed for $REMOTE_HOST:$REMOTE_ROOT" >&2
  fi
  echo "Backed up runtime DBs to $REMOTE_HOST:$remote_dest"
  exit 0
fi

mv "$staging_dir" "$dest_dir"
trap - EXIT

if ! prune_snapshot_root "$BACKUP_ROOT" "$KEEP_DAYS" "$KEEP_COUNT"; then
  echo "Warning: backup retention prune failed for $BACKUP_ROOT" >&2
fi

echo "Backed up runtime DBs to $dest_dir"
