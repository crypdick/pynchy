#!/bin/sh
set -eu

: "${GH_TOKEN:?set GH_TOKEN}"
: "${PYNCHY_RELEASE_REPOSITORY:?set PYNCHY_RELEASE_REPOSITORY}"
: "${PYNCHY_RELEASE_HOST_IMAGE:?set PYNCHY_RELEASE_HOST_IMAGE}"
: "${PYNCHY_RELEASE_AGENT_IMAGE:?set PYNCHY_RELEASE_AGENT_IMAGE}"

namespace=${PYNCHY_KUBERNETES_NAMESPACE:-pynchy}
checkout=${PYNCHY_RELEASE_CHECKOUT:-/srv/pynchy/app}
release_patch=${PYNCHY_RELEASE_PATCH:-}
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

published_release() {
    gh api --method GET \
        "repos/$PYNCHY_RELEASE_REPOSITORY/actions/workflows/test.yml/runs" \
        -f branch=main \
        -f event=push \
        -f status=success \
        -f per_page=1 \
        --jq '.workflow_runs[0].head_sha // empty'
}

valid_sha() {
    printf '%s' "$1" | jq -R -e 'test("^[0-9a-f]{40}$")' >/dev/null
}

target_sha=$(published_release)
if [ -z "$target_sha" ]; then
    printf 'No successful main release is published yet.\n'
    exit 0
fi
if ! valid_sha "$target_sha"; then
    printf 'Invalid published revision: %s\n' "$target_sha" >&2
    exit 1
fi

current_sha=$(k get deployment pynchy \
    -o 'jsonpath={.spec.template.metadata.annotations.pynchy\.dev/release-sha}')
desktop_sha=$(k get deployment pynchy-desktop \
    -o 'jsonpath={.spec.template.metadata.annotations.pynchy\.dev/release-sha}')
monitor_sha=$(k get cronjob pynchy-release-monitor \
    -o 'jsonpath={.metadata.annotations.pynchy\.dev/release-sha}')
if [ "$current_sha" = "$target_sha" ] \
    && [ "$desktop_sha" = "$target_sha" ] \
    && [ "$monitor_sha" = "$target_sha" ]; then
    printf 'Release %s already active.\n' "$target_sha"
    exit 0
fi
if ! valid_sha "$current_sha"; then
    printf 'Current Pynchy Deployment has no valid release SHA.\n' >&2
    exit 1
fi

short_sha=$(printf '%s' "$target_sha" | cut -c1-12)
host_probe="pynchy-host-preflight-$short_sha"
agent_probe="pynchy-agent-preflight-$short_sha"
release_root=$(mktemp -d /tmp/pynchy-release.XXXXXX)
# Invoked by trap.
# shellcheck disable=SC2329
cleanup() {
    k delete pod "$host_probe" "$agent_probe" \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
    rm -rf "$release_root"
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
    if k wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=300s; then
        return 0
    fi

    reason=$(k get pod "$name" -o json | jq -r '
        [(.status.initContainerStatuses // [])[], (.status.containerStatuses // [])[]]
        | map(.state.waiting.reason // empty)
        | first // empty
    ')
    case "$reason" in
        ContainerCreating|ErrImagePull|ImagePullBackOff)
            printf 'Waiting for release image %s.\n' "$image"
            return 75
            ;;
    esac
    k describe pod "$name" >&2 || true
    k logs "$name" >&2 || true
    return 1
}

host_image="$PYNCHY_RELEASE_HOST_IMAGE:$target_sha"
agent_image="$PYNCHY_RELEASE_AGENT_IMAGE:$target_sha"
preflight_status=0
preflight "$host_probe" "$host_image" \
    /opt/pynchy/.venv/bin/python -c 'import pynchy' || preflight_status=$?
if [ "$preflight_status" -ne 0 ]; then
    [ "$preflight_status" -eq 75 ] && exit 0
    exit "$preflight_status"
fi
preflight_status=0
preflight "$agent_probe" "$agent_image" python -c 'import agent_runner' \
    || preflight_status=$?
if [ "$preflight_status" -ne 0 ]; then
    [ "$preflight_status" -eq 75 ] && exit 0
    exit "$preflight_status"
fi

latest_sha=$(published_release)
if [ "$latest_sha" != "$target_sha" ]; then
    printf 'A newer successful release appeared during preflight; deferring %s.\n' "$target_sha"
    exit 0
fi

render_application() {
    source_root=$1
    sha=$2
    manifest=$3
    overlay=$4
    host="$PYNCHY_RELEASE_HOST_IMAGE:$sha"
    agent="$PYNCHY_RELEASE_AGENT_IMAGE:$sha"
    mkdir -p "$overlay"

    jq -n \
        --arg resource "$source_root/deploy/k3s/application" \
        --arg patch "$release_patch" '
        {
            apiVersion: "kustomize.config.k8s.io/v1beta1",
            kind: "Kustomization",
            resources: [$resource],
            patches: ([
                {path: "pynchy-release.yaml"},
                {path: "desktop-release.yaml"},
                {path: "monitor-release.yaml"}
            ] + (if $patch == "" then [] else [{path: $patch}] end))
        }
    ' > "$overlay/kustomization.yaml"
    jq -n --arg sha "$sha" --arg host "$host" --arg agent "$agent" '
        {
            apiVersion: "apps/v1",
            kind: "Deployment",
            metadata: {name: "pynchy", namespace: "pynchy"},
            spec: {template: {
                metadata: {annotations: {"pynchy.dev/release-sha": $sha}},
                spec: {containers: [{
                    name: "pynchy",
                    image: $host,
                    env: [{name: "CONTAINER__IMAGE", value: $agent}]
                }]}
            }}
        }
    ' > "$overlay/pynchy-release.yaml"
    jq -n --arg sha "$sha" --arg host "$host" '
        {
            apiVersion: "apps/v1",
            kind: "Deployment",
            metadata: {name: "pynchy-desktop", namespace: "pynchy"},
            spec: {template: {
                metadata: {annotations: {"pynchy.dev/release-sha": $sha}},
                spec: {containers: [{name: "desktop", image: $host}]}
            }}
        }
    ' > "$overlay/desktop-release.yaml"
    jq -n --arg sha "$sha" --arg host "$host" '
        {
            apiVersion: "batch/v1",
            kind: "CronJob",
            metadata: {
                name: "pynchy-release-monitor",
                namespace: "pynchy",
                annotations: {"pynchy.dev/release-sha": $sha}
            },
            spec: {jobTemplate: {spec: {template: {
                metadata: {annotations: {"pynchy.dev/release-sha": $sha}},
                spec: {containers: [{name: "monitor", image: $host}]}
            }}}}
        }
    ' > "$overlay/monitor-release.yaml"

    kubectl --kubeconfig "$kubeconfig" kustomize "$overlay" \
        --load-restrictor LoadRestrictionsNone > "$manifest"
}

if [ ! -d "$checkout/deploy/k3s/application" ]; then
    printf 'Release checkout lacks deploy/k3s/application; apply bootstrap once.\n' >&2
    exit 1
fi
if [ -n "$release_patch" ] && [ ! -f "$release_patch" ]; then
    printf 'Release patch does not exist: %s\n' "$release_patch" >&2
    exit 1
fi

checkout_sha=$(git -C "$checkout" rev-parse HEAD)
if [ "$checkout_sha" != "$current_sha" ]; then
    printf 'Release checkout %s does not match active release %s.\n' \
        "$checkout_sha" "$current_sha" >&2
    exit 1
fi
if ! git -C "$checkout" diff --quiet --ignore-submodules -- \
    || ! git -C "$checkout" diff --cached --quiet --ignore-submodules --; then
    printf 'Release checkout has tracked changes; refusing to replace them.\n' >&2
    exit 1
fi

previous_manifest="$release_root/previous.yaml"
target_manifest="$release_root/target.yaml"
render_application "$checkout" "$current_sha" "$previous_manifest" \
    "$release_root/previous-overlay"

checkout_advanced=false
if [ "$checkout_sha" != "$target_sha" ]; then
    gh auth setup-git >/dev/null
    git -C "$checkout" fetch --quiet origin "$target_sha"
    if ! git -C "$checkout" merge-base --is-ancestor "$checkout_sha" "$target_sha"; then
        printf 'Published release %s is not a fast-forward from %s.\n' \
            "$target_sha" "$checkout_sha" >&2
        exit 1
    fi
    git -C "$checkout" merge --ff-only --quiet "$target_sha"
    checkout_advanced=true
fi
if [ "$(git -C "$checkout" rev-parse HEAD)" != "$target_sha" ]; then
    printf 'Release checkout did not reach %s.\n' "$target_sha" >&2
    exit 1
fi
render_application "$checkout" "$target_sha" "$target_manifest" \
    "$release_root/target-overlay"

rollout_application() {
    k rollout status statefulset/pynchy-postgres --timeout=300s \
        && k rollout status statefulset/pynchy-temporal --timeout=300s \
        && k rollout status deployment/pynchy --timeout=300s \
        && k rollout status deployment/pynchy-desktop --timeout=300s
}

rollback() {
    printf 'Release %s failed; restoring %s.\n' "$target_sha" "$current_sha" >&2
    rollback_status=0
    k apply -f "$previous_manifest" >/dev/null || rollback_status=1
    if [ "$checkout_advanced" = true ]; then
        git -C "$checkout" reset --keep "$current_sha" || rollback_status=1
    fi
    rollout_application || rollback_status=1
    return "$rollback_status"
}

if ! k apply -f "$target_manifest" >/dev/null; then
    rollback || true
    exit 1
fi
if ! rollout_application; then
    rollback || true
    exit 1
fi

attempt=0
while [ "$attempt" -lt 12 ]; do
    status=$(k exec deployment/pynchy -c pynchy -- \
        /opt/pynchy/.venv/bin/pynchy status 2>/dev/null || true)
    desktop_status=$(printf '{"action":"check_permissions"}' \
        | k exec -i deployment/pynchy-desktop -c desktop -- \
            pynchy-x11-computer-use 2>/dev/null || true)
    if printf '%s' "$status" | jq -e --arg sha "$target_sha" '
        .service.status == "ok"
        and .deploy.head_sha == $sha
        and .deploy.last_deploy_sha == $sha
        and .gateway.ready == true
        and .temporal.cluster_healthy == true
        and .temporal.worker_running == true
    ' >/dev/null 2>&1 \
        && printf '%s' "$desktop_status" | jq -e \
            '.ready == true and .protocol_version == 1' >/dev/null 2>&1; then
        printf 'Released %s.\n' "$target_sha"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 5
done

rollback || true
exit 1
