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

exec /usr/lib/protonmail/bridge/bridge "$@"
