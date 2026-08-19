#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly namespace=pynchy
readonly postgres_pod=pynchy-postgres-0
: "${PYNCHY_K3S_STORAGE_ROOT:?set PYNCHY_K3S_STORAGE_ROOT}"
readonly data_dir="$PYNCHY_K3S_STORAGE_ROOT/shared/app/data"
readonly postgres_volume="$PYNCHY_K3S_STORAGE_ROOT/postgres"
readonly vaultwarden_volume="$PYNCHY_K3S_STORAGE_ROOT/vaultwarden"
readonly backup_root="$PYNCHY_K3S_STORAGE_ROOT/backups"
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

if [[ -f "$vaultwarden_volume/db.sqlite3" ]]; then
    mkdir "$partial/vaultwarden"
    rsync -a --exclude 'db.sqlite3*' "$vaultwarden_volume/" "$partial/vaultwarden/"
    sqlite3 "$vaultwarden_volume/db.sqlite3" \
        ".timeout 5000" ".backup '$partial/vaultwarden/db.sqlite3'"
    [[ $(sqlite3 "$partial/vaultwarden/db.sqlite3" "PRAGMA quick_check;") == ok ]]
fi

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
    find . -type f ! -path ./SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
)
chown -R 568:568 "$partial"
find "$partial" -type d -exec chmod 0750 {} +
find "$partial" -type f -exec chmod 0640 {} +
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
