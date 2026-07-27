# LiteLLM Diagnostics Reference

All commands assume you are on the Pynchy host. When running remotely, set `PYNCHY_HOST` to the deployment-specific hostname and use `ssh "$PYNCHY_HOST" '<command>'`.

## Health & readiness

```bash
# Model deployment health (lists healthy/unhealthy endpoints)
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/health

# DB + cache connectivity
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/health/readiness
```

## Available models

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/v1/models
```

With wildcard routing, this lists all models the provider supports.

## Request evidence

Do not call LiteLLM's `/spend/logs` or `/global/spend/logs` routes. A bounded
request to the spend-log route exhausted the proxy container on the live
deployment. Query Pynchy's `/status` endpoint, inspect bounded container logs,
and use the Pynchy SQLite message and trace queries in the main Pynchy Ops skill
instead. Use the LiteLLM dashboard only for narrow, interactive spend review.

## Virtual keys

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/key/list
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/key/info?key=<key_hash>"
```

## Container logs

```bash
docker logs pynchy-litellm --since 15m --tail 300 2>&1 | grep -i "error\|fail\|exception"
docker logs pynchy-litellm --since 15m --tail 100
```

## Common failure patterns

| Error class | Meaning | Fix |
|---|---|---|
| `ProxyModelNotFoundError` | Model not in config | Use wildcard routing (`anthropic/*`) or add model explicitly |
| `BadRequestError` + "no healthy deployments" | All deployments in cooldown or failed health probes | See "Failover & cooldown" below |
| `BaseLLMException` + "rate_limit_error" | Account quota exhausted or RPM/TPM limit hit | If persistent, check account spend; see "Failover & cooldown" |
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

**Pynchy's fix**: At startup, `gateway.py:_prepare_config()` filters the config before mounting it into LiteLLM. Model entries whose `api_key` env var is unset or matches a placeholder pattern (`...`, `YOUR_`, `CHANGE_ME`, etc.) are removed. Check pynchy logs for warnings like:

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

```bash
# Check deployment health
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/health | python3 -m json.tool

# Check recent bounded proxy errors
docker logs pynchy-litellm --since 15m --tail 300 2>&1 | grep -i "cooldown\|429\|rate.limit\|no healthy"

# Check which deployments LiteLLM loaded
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/v1/model/info | python3 -c "
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
