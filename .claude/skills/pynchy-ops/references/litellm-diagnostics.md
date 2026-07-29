# LiteLLM Diagnostics Reference

Use this reference to collect bounded, low-risk LiteLLM evidence during an incident. All commands assume you are on the Pynchy host. When running remotely, set `PYNCHY_HOST` to the deployment-specific hostname and use `ssh "$PYNCHY_HOST" '<command>'`.

Set `KEY` through an approved secret mechanism in the current shell. Do not echo, log, or paste the master key into command output or shell history.

## Routine proxy and database readiness

Use authenticated `/health/readiness` for routine proxy and database liveness. It proves proxy and database readiness only; it does not prove a configured model can serve a request.

```bash
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  http://localhost:4000/health/readiness
```

Do not use `/health` as a routine check. It can perform provider model calls and emit raw diagnostic structures, so it is excluded from routine diagnostics.

## Model configuration inspection

```bash
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  http://localhost:4000/v1/models

curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  http://localhost:4000/v1/model/info
```

Use `/v1/models` and `/v1/model/info` only to inspect exposed model configuration. They do not prove that a provider request path is live.

## Spend-log quarantine

`GET /spend/logs` is unsafe for routine live diagnostics regardless of requested limit or filters. Do not call it until a separate verified LiteLLM repair establishes a caller-independent resource bound.

A bounded request to this route exhausted the proxy container on the live deployment.

`GET /global/spend/logs` is not validated as a substitute and is excluded from routine diagnostics.

Do not use a small limit, filters, or the global route as a workaround. Preserve bounded logs and request identifiers for later offline analysis instead.

## Virtual keys

```bash
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  http://localhost:4000/key/list

curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  "http://localhost:4000/key/info?key=<key_hash>"
```

## Bounded lifecycle logs

Bound both the lookback window and record count before examining lifecycle evidence.

```bash
docker logs --since 15m --tail 200 pynchy-litellm 2>&1

docker logs --since 5m --tail 100 pynchy-litellm 2>&1
```

Inspect bounded output directly and preserve any Docker error with the incident evidence. Record the bounded window, container name, request identifiers, and timestamps. Do not increase bounds until the existing evidence has been reviewed.

## Optional configured-model SSE canary

Run this only after readiness succeeds and only with a configured model name. It makes a real provider request and can incur provider cost. It proves post-recovery request-path liveness only; it does not identify or prove an OOM cause.

```bash
(
  CANARY_MODEL="<configured-model>"

  curl --silent --show-error --no-buffer \
    --connect-timeout 2 --max-time 15 \
    --write-out "\n__PYNCHY_CANARY_STATUS__:%{http_code}\n" \
    -H "Authorization: Bearer $KEY" \
    -H "Accept: text/event-stream" \
    -H "Content-Type: application/json" \
    --data "{\"model\":\"$CANARY_MODEL\",\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\".\"}]}],\"stream\":true,\"max_output_tokens\":1}" \
    http://localhost:4000/v1/responses |
    awk '
      { sub(/\r$/, "") }
      /^__PYNCHY_CANARY_STATUS__:/ {
        status = substr($0, length("__PYNCHY_CANARY_STATUS__:") + 1)
        next
      }
      NF { terminal_done = ($0 == "data: [DONE]") }
      END { exit (status == "200" && terminal_done) ? 0 : 1 }
    '
)
```

Require HTTP 200 and `data: [DONE]` as the final nonempty SSE line. The stream parser retains only the status and terminal marker; it never writes SSE data to disk or output. Use `/v1/model/info` to select a configured model; do not turn this canary into a retry loop.

## SYN-94 `stream_disconnected` correlation

Keep SYN-94 `stream_disconnected` investigation separate from this spend-log containment. Correlate bounded proxy logs, client timestamps, and request identifiers, but do not make a causal claim from their co-occurrence. The optional SSE canary can establish request-path liveness after recovery; it cannot establish why a prior stream disconnected.

## Common failure patterns

| Error class | Meaning | Fix |
|---|---|---|
| `ProxyModelNotFoundError` | Model not in config | Use wildcard routing (`anthropic/*`) or add model explicitly |
| `BadRequestError` + "no healthy deployments" | All deployments in cooldown or failed health probes | See "Failover & cooldown" below |
| `BaseLLMException` + "rate_limit_error" | Account quota exhausted or RPM/TPM limit hit | If persistent, inspect approved provider billing and bounded lifecycle logs; see "Failover & cooldown" |
| `BaseLLMException` + "OAuth token has expired" | Token expired between refreshes | Transient; retries handle it. If persistent, run `claude setup-token` on server |
| `BaseLLMException` + "x-api-key header is required" | Auth header missing during key rotation | Transient; resolves on retry |
| `BaseLLMException` + "invalid x-api-key" | Invalid/placeholder key or stale key after rotation | Check .env; if placeholder, pynchy should filter it at startup |
| `BaseLLMException` + "missing anthropic-beta header" | OAuth token used but LiteLLM too old for server-side OAuth | Upgrade LiteLLM to a build including PR #21039 (server-side OAuth detection) |

## Failover & cooldown

### How LiteLLM multi-key failover works

When multiple `model_list` entries share the same `model_name` (e.g. two `anthropic/*` entries with different API keys), LiteLLM's router distributes requests across them. When one fails, two mechanisms handle failover:

1. **Retries** (`num_retries`): On failure, the router retries the request on a different deployment within the same model group.
2. **Cooldowns** (`cooldown_time`): LiteLLM decides when to remove an unhealthy deployment from rotation, then keeps it out for the configured duration.

Retries cannot provide failover when a model group contains only one deployment. Keep `num_retries: 0` for that topology unless another client-independent reason requires repeated calls to the same upstream.

### Why `allowed_fails` stays unset

Leave `allowed_fails` unset for the normal Pynchy configuration. LiteLLM's topology-aware policy avoids cooling the only deployment in a model group for ordinary retryable failures. It can cool a failing deployment more aggressively when the group has an alternate route.

Setting a non-default `allowed_fails` value opts into a fixed failure counter instead. For example, `allowed_fails: 1` cools a deployment after its second eligible failure during the counter window. On a single-deployment model group, one client reconnect sequence can then turn a transient upstream overload into a full `cooldown_time` outage.

### Startup health probes (the hidden gotcha)

At startup, LiteLLM runs internal health probes against all deployments. These probes are real API calls (tagged `litellm-internal-health-check`) that:
- Test model availability by calling small requests against various model names
- Count toward a fixed `allowed_fails` failure counter when configured
- Can mark deployments as unhealthy if they fail

**Impact**: Health probes can report a deployment as unhealthy even when proxy readiness succeeds. Avoid fixed, aggressive `allowed_fails` thresholds that can turn transient probe or provider failures into a cooldown for every deployment in a model group.

### Zombie deployments (filtered by pynchy)

A "zombie deployment" occurs when `litellm_config.yaml` references an env var that is unset or contains a placeholder value (e.g. `sk-ant-...`). LiteLLM loads the deployment with an invalid key. The result:

- Startup health probes fail with 401 (auth error)
- The router marks the deployment as unhealthy
- Retries burn attempts on the dead deployment before failing over
- `usage-based-routing` keeps picking the dead deployment because it has zero usage

**Pynchy's fix**: At startup, Pynchy filters the generated LiteLLM config before mounting it. Model entries whose `api_key` env var is unset or matches a placeholder pattern (`...`, `YOUR_`, `CHANGE_ME`, etc.) are removed. Check Pynchy logs for warnings like:

```
Removing model entry with placeholder api_key  model_id=anthropic-employee2  var=ANTHROPIC_TOKEN_EMPLOYEE2
```

### Recommended `router_settings`

```yaml
router_settings:
  routing_strategy: usage-based-routing
  # One deployment per model_name: retries have no alternate route.
  num_retries: 0
  # Intentionally omit allowed_fails to preserve topology-aware cooldowns.
  cooldown_time: 600  # 10 min
```

When a model group has multiple deployments, increase `num_retries` to the desired number of failover attempts. `cooldown_time` controls the exclusion duration after LiteLLM decides to cool a deployment; it does not force a cooldown by itself.

### Diagnosing failover issues

1. Confirm `/health/readiness` before investigating routes.
2. Review the bounded lifecycle logs for the affected window and retain request identifiers.
3. Inspect the loaded model metadata without making provider calls:

```bash
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
  -H "Authorization: Bearer $KEY" \
  http://localhost:4000/v1/model/info | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(f\"  {m['model_info'].get('id','?'):30s} model_name={m['model_name']}\")
"
```

### Config options that DON'T work

| Setting | Status | Notes |
|---|---|---|
| `retry_on_status_codes: [429]` | **Rejected** | Not a valid `Router.__init__()` argument in LiteLLM 1.81.x |
| `disable_cooldowns: true` | Accepted but **insufficient** | Disables the cooldown mechanism but does NOT prevent startup health probes from marking deployments unhealthy |
