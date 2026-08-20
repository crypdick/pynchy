#!/bin/sh
set -eu

gnupg_home=${GNUPGHOME:-$HOME/.gnupg}
password_store=${PASSWORD_STORE_DIR:-$HOME/.password-store}
export GNUPGHOME="$gnupg_home"
export PASSWORD_STORE_DIR="$password_store"

mkdir -p "$gnupg_home" "$password_store"
chmod 700 "$gnupg_home" "$password_store"

if [ ! -s "$password_store/.gpg-id" ]; then
    gpg --batch --passphrase '' \
        --quick-generate-key 'Pynchy Proton Bridge' default default never
    fingerprint=$(gpg --batch --with-colons --list-secret-keys \
        | awk -F: '$1 == "fpr" { print $10; exit }')
    pass init "$fingerprint"
fi

bridge=/usr/lib/protonmail/bridge/bridge
enrollment_lock=$HOME/.pynchy-proton-bridge-enrollment
pid_file=$HOME/.pynchy-proton-bridge.pid

if [ "${1:-}" = enroll ]; then
    mkdir "$enrollment_lock"
    trap 'rmdir "$enrollment_lock"' EXIT HUP INT TERM
    if [ -s "$pid_file" ]; then
        kill "$(cat "$pid_file")" 2>/dev/null || true
    fi
    while [ -e "$pid_file" ]; do
        sleep 1
    done
    "$bridge" --cli
    exit
fi

if [ "$#" -ne 1 ] || [ "$1" != --noninteractive ]; then
    exec "$bridge" "$@"
fi

bridge_pid=
stop_bridge() {
    if [ -n "$bridge_pid" ]; then
        kill "$bridge_pid" 2>/dev/null || true
        wait "$bridge_pid" 2>/dev/null || true
    fi
    exit 0
}
trap stop_bridge HUP INT TERM

while :; do
    while [ -d "$enrollment_lock" ]; do
        sleep 1
    done
    "$bridge" --noninteractive &
    bridge_pid=$!
    printf '%s\n' "$bridge_pid" > "$pid_file"
    if wait "$bridge_pid"; then
        bridge_exit=0
    else
        bridge_exit=$?
    fi
    bridge_pid=
    rm -f "$pid_file"
    if [ ! -d "$enrollment_lock" ]; then
        exit "$bridge_exit"
    fi
done
