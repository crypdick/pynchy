#!/bin/sh
set -eu

: "${GH_TOKEN:?set GH_TOKEN}"
: "${PYNCHY_RELEASE_REPOSITORY:?set PYNCHY_RELEASE_REPOSITORY}"
: "${PYNCHY_RELEASE_HOST_IMAGE:?set PYNCHY_RELEASE_HOST_IMAGE}"
: "${PYNCHY_RELEASE_AGENT_IMAGE:?set PYNCHY_RELEASE_AGENT_IMAGE}"

namespace=${PYNCHY_KUBERNETES_NAMESPACE:-pynchy}
service_account_root=/var/run/secrets/kubernetes.io/serviceaccount
kubeconfig=${PYNCHY_KUBECONFIG:-/tmp/pynchy-release-kubeconfig.json}

if [ -z "${PYNCHY_KUBECONFIG:-}" ]; then
    token=$(cat "$service_account_root/token")
    jq -n \
        --arg server "https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT_HTTPS" \
        --arg certificate_authority "$service_account_root/ca.crt" \
        --arg token "$token" \
        --arg namespace "$namespace" \
        '{
            apiVersion: "v1",
            kind: "Config",
            clusters: [{name: "cluster", cluster: {
                server: $server,
                "certificate-authority": $certificate_authority
            }}],
            users: [{name: "monitor", user: {token: $token}}],
            contexts: [{name: "monitor", context: {
                cluster: "cluster",
                user: "monitor",
                namespace: $namespace
            }}],
            "current-context": "monitor"
        }' > "$kubeconfig"
    chmod 600 "$kubeconfig"
fi

k() {
    kubectl --kubeconfig "$kubeconfig" -n "$namespace" "$@"
}

target_sha=$(gh api "repos/$PYNCHY_RELEASE_REPOSITORY/commits/main" --jq .sha)
if ! printf '%s' "$target_sha" | jq -R -e 'test("^[0-9a-f]{40}$")' >/dev/null; then
    printf 'Invalid main revision: %s\n' "$target_sha" >&2
    exit 1
fi

current_sha=$(k get deployment pynchy \
    -o 'jsonpath={.spec.template.metadata.annotations.pynchy\.dev/release-sha}')
if [ "$current_sha" = "$target_sha" ]; then
    printf 'Release %s already active.\n' "$target_sha"
    exit 0
fi

checkout_status=$(k exec deployment/pynchy -c pynchy -- \
    /opt/pynchy/.venv/bin/pynchy status)
if ! printf '%s' "$checkout_status" | jq -e --arg sha "$target_sha" \
    '.deploy.head_sha == $sha' >/dev/null; then
    printf 'Waiting for checkout %s.\n' "$target_sha"
    exit 0
fi

short_sha=$(printf '%s' "$target_sha" | cut -c1-12)
host_probe="pynchy-host-preflight-$short_sha"
agent_probe="pynchy-agent-preflight-$short_sha"
cleanup() {
    k delete pod "$host_probe" "$agent_probe" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

preflight() {
    name=$1
    image=$2
    shift 2
    k delete pod "$name" --ignore-not-found --wait=false >/dev/null
    k run "$name" \
        --restart=Never \
        --image="$image" \
        --image-pull-policy=IfNotPresent \
        --overrides='{"spec":{"serviceAccountName":"pynchy-release-monitor","automountServiceAccountToken":false}}' \
        --command -- "$@" >/dev/null
    if ! k wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=300s; then
        k describe pod "$name" >&2 || true
        k logs "$name" >&2 || true
        return 1
    fi
}

host_image="$PYNCHY_RELEASE_HOST_IMAGE:$target_sha"
agent_image="$PYNCHY_RELEASE_AGENT_IMAGE:$target_sha"
preflight "$host_probe" "$host_image" /opt/pynchy/.venv/bin/python -c 'import pynchy'
preflight "$agent_probe" "$agent_image" python -c 'import agent_runner'

latest_sha=$(gh api "repos/$PYNCHY_RELEASE_REPOSITORY/commits/main" --jq .sha)
if [ "$latest_sha" != "$target_sha" ]; then
    printf 'Main advanced during preflight; deferring %s.\n' "$target_sha"
    exit 0
fi

patch=$(jq -cn \
    --arg sha "$target_sha" \
    --arg host_image "$host_image" \
    --arg agent_image "$agent_image" \
    '{
        spec: {template: {
            metadata: {annotations: {"pynchy.dev/release-sha": $sha}},
            spec: {containers: [{
                name: "pynchy",
                image: $host_image,
                env: [{name: "CONTAINER__IMAGE", value: $agent_image}]
            }]}
        }}
    }')
k patch deployment pynchy --type=strategic -p "$patch" >/dev/null

rollback() {
    printf 'Release %s failed; rolling back.\n' "$target_sha" >&2
    k rollout undo deployment/pynchy >/dev/null
    k rollout status deployment/pynchy --timeout=300s
}

if ! k rollout status deployment/pynchy --timeout=300s; then
    rollback
    exit 1
fi

attempt=0
while [ "$attempt" -lt 12 ]; do
    status=$(k exec deployment/pynchy -c pynchy -- \
        /opt/pynchy/.venv/bin/pynchy status 2>/dev/null || true)
    if printf '%s' "$status" | jq -e --arg sha "$target_sha" '
        .service.status == "ok"
        and .deploy.last_deploy_sha == $sha
        and .gateway.ready == true
        and .temporal.cluster_healthy == true
        and .temporal.worker_running == true
    ' >/dev/null 2>&1; then
        printf 'Released %s.\n' "$target_sha"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 5
done

rollback
exit 1
