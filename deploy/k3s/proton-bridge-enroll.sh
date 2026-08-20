#!/bin/sh
set -eu

namespace=${PYNCHY_KUBERNETES_NAMESPACE:-pynchy}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
checkout=$(CDPATH= cd -- "$script_dir/../.." && pwd)
personalization=${PYNCHY_PERSONALIZATION_ROOT:-$checkout/data/personalization}
secret_overlay=${PYNCHY_PROTON_BRIDGE_SECRET_OVERLAY:-$personalization/ops/k3s/proton-bridge-secret}
secret_file=$secret_overlay/proton-bridge.env
mounted_secret=/var/run/secrets/proton-bridge/password
bridge_output=
secret_tmp=
password=
resume=false

cleanup() {
    if [ -n "$bridge_output" ]; then
        rm -f "$bridge_output"
    fi
    if [ -n "$secret_tmp" ]; then
        rm -f "$secret_tmp"
    fi
    password=
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

case "$#" in
    0) ;;
    1)
        [ "$1" = --resume ] || die "Usage: $0 [--resume]"
        resume=true
        ;;
    *) die "Usage: $0 [--resume]" ;;
esac

privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

k() {
    privileged k3s kubectl -n "$namespace" "$@"
}

bridge_commands() {
    attempt=0
    while [ "$attempt" -lt 15 ]; do
        sleep 2
        printf 'info\r'
        attempt=$((attempt + 1))
    done
    printf 'updates autoupdates disable\r'
    sleep 1
    printf 'y\r'
    sleep 1
    printf 'exit\r'
}

[ -f "$secret_overlay/kustomization.yaml" ] \
    || die "Missing Proton Bridge Secret overlay: $secret_overlay"
git -c safe.directory="$personalization" -C "$personalization" check-ignore -q "$secret_file" \
    || die "Refusing to write an app password to a path not ignored by Git."

containers=$(k get deployment pynchy \
    -o 'jsonpath={range .spec.template.spec.containers[*]}{.name}{" "}{end}')
case " $containers " in
    *" proton-bridge "*) ;;
    *) die "Deployment pynchy has no proton-bridge sidecar." ;;
esac
case "$namespace" in
    ""|*[!a-z0-9.-]*) die "Invalid Kubernetes namespace: $namespace" ;;
esac
command -v script >/dev/null 2>&1 || die "Missing required host command: script"

if [ "$resume" = false ]; then
    printf '%s\n' \
        "Bridge CLI will open." \
        "Run: login" \
        "Wait for the initial sync, then run: exit" \
        "The helper captures the Bridge app password without displaying it."
    k exec -it deployment/pynchy -c proton-bridge -- pynchy-proton-bridge enroll
fi

umask 077
bridge_output=$(mktemp)
bridge_cli="k3s kubectl -n $namespace exec -it deployment/pynchy -c proton-bridge -- pynchy-proton-bridge enroll"
bridge_commands \
    | privileged script -qec "$bridge_cli" /dev/null \
        >"$bridge_output" 2>&1 \
    || die "Could not read the enrolled Bridge account."
password=$(tr -d '\r' < "$bridge_output" \
    | sed -n 's/.*Password:[[:space:]]*//p' \
    | head -n 1)
[ -n "$password" ] || die "Bridge has no enrolled account. Run without --resume to log in."
rm -f "$bridge_output"
bridge_output=

secret_tmp=$(mktemp)
printf 'password=%s\n' "$password" > "$secret_tmp"
password=
owner=$(stat -c %u "$secret_overlay")
group=$(stat -c %g "$secret_overlay")
privileged install -m 0600 "$secret_tmp" "$secret_file"
privileged chown "$owner:$group" "$secret_file"
rm -f "$secret_tmp"
secret_tmp=

k apply -k "$secret_overlay" >/dev/null

attempt=0
until k exec deployment/pynchy -c pynchy -- test -s "$mounted_secret" \
    >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 30 ] || die "Timed out waiting for the projected Bridge Secret."
    sleep 2
done

k exec deployment/pynchy -c proton-bridge -- sh -c '
    test -s /home/bridge/.pynchy-proton-bridge.pid
    kill -0 "$(cat /home/bridge/.pynchy-proton-bridge.pid)"
    grep -q ":0401 " /proc/net/tcp
    grep -q ":0477 " /proc/net/tcp
' >/dev/null
k exec deployment/pynchy -c pynchy -- /opt/pynchy/.venv/bin/python -c '
from pynchy.plugins.integrations.api import create_proton_mail_client
create_proton_mail_client().list_mailboxes()
' >/dev/null

printf '%s\n' "Proton Bridge enrollment complete."
