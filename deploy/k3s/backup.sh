#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly namespace=pynchy
readonly postgres_pod=pynchy-postgres-0
readonly data_dir=/mnt/tank-20/appdata/pynchy-k3s/shared/app/data
readonly postgres_volume=/mnt/tank-20/appdata/pynchy-k3s/postgres
readonly backup_root=/mnt/tank-20/appdata/pynchy-k3s/backups
readonly keep=14
readonly timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly partial="$backup_root/.partial-$timestamp"
readonly destination="$backup_root/$timestamp"
readonly postgres_staging="/var/lib/postgresql/data/.pynchy-backup-$timestamp"

cleanup() {
    status=$?
    trap - EXIT
    k3s kubectl -n "$namespace" exec "$postgres_pod" -- \
        rm -rf -- "$postgres_staging" >/dev/null 2>&1 || true
    if ((status != 0)); then
        echo "Incomplete backup retained at $partial" >&2
    fi
    exit "$status"
}

mkdir -p "$backup_root"
mkdir "$partial"
trap cleanup EXIT

for database in messages.db neonize.db; do
    sqlite3 "$data_dir/$database" ".timeout 5000" ".backup '$partial/$database'"
    [[ $(sqlite3 "$partial/$database" "PRAGMA quick_check;") == ok ]]
done

postgres_user=$(k3s kubectl -n "$namespace" get secret pynchy-runtime \
    -o jsonpath='{.data.POSTGRES_USER}' | base64 -d)
k3s kubectl -n "$namespace" exec "$postgres_pod" -- mkdir -m 0700 "$postgres_staging"
for database in litellm temporal temporal_visibility; do
    k3s kubectl -n "$namespace" exec "$postgres_pod" -- \
        pg_dump -U "$postgres_user" -d "$database" -Fc \
        -f "$postgres_staging/$database.dump"
    k3s kubectl -n "$namespace" exec "$postgres_pod" -- \
        pg_restore -l "$postgres_staging/$database.dump" >/dev/null
    install -m 0600 \
        "$postgres_volume/.pynchy-backup-$timestamp/$database.dump" \
        "$partial/$database.dump"
done
k3s kubectl -n "$namespace" exec "$postgres_pod" -- rm -rf -- "$postgres_staging"

(
    cd "$partial"
    sha256sum ./* >SHA256SUMS
)
chown -R 568:568 "$partial"
chmod 0750 "$partial"
chmod 0640 "$partial"/*
mv "$partial" "$destination"
trap - EXIT

mapfile -t generations < <(
    find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*T*Z' -printf '%p\n' |
        sort -r
)
for old_generation in "${generations[@]:$keep}"; do
    find "$old_generation" -type f -delete
    find "$old_generation" -depth -type d -empty -delete
done

echo "Backed up Pynchy K3s state to $destination"
