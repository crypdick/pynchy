# LiteLLM Diagnostics Reference

All commands assume SSH to pynchy-server. Use `ssh pynchy-server '<command>'` when running remotely.

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

## Spend logs (primary diagnostic tool)

```bash
# Recent requests (success + failure)
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/spend/logs?limit=100"

# Failures only
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/spend/logs?request_status=failure&limit=50"
```

Each entry contains: `request_id`, `model`, `status`, `startTime`, `endTime`, `spend`, `total_tokens`, and `metadata.error_information` (with `error_class`, `error_code`, `error_message`).

### Failure analysis pattern

```bash
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/spend/logs?limit=500" | python3 -c "
import sys, json
from collections import Counter
data = json.load(sys.stdin)
failures = [r for r in data if r.get('status') == 'failure']
print(f'Total: {len(data)}, Failures: {len(failures)}, Rate: {len(failures)/len(data)*100:.1f}%')
print('\nFailure models:')
for m, c in Counter(r.get('model','?') for r in failures).most_common():
    print(f'  {m}: {c}')
print('\nError classes:')
for f in failures[:5]:
    err = f.get('metadata',{}).get('error_information',{})
    print(f'  {err.get(\"error_class\")}: {str(err.get(\"error_message\",\"\"))[:120]}')
"
```

## Global spend

```bash
# By date range
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/global/spend/logs?start_date=2026-02-01&end_date=2026-02-28"

# By provider
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/global/spend/provider"
```

## Virtual keys

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/key/list
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/key/info?key=<key_hash>"
```

## Container logs

```bash
docker logs pynchy-litellm --since 1h 2>&1 | grep -i "error\|fail\|exception"
docker logs pynchy-litellm --since 30m 2>&1 | tail -100
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

## Failover & cooldown

### How LiteLLM multi-key failover works

When multiple `model_list` entries share the same `model_name` (e.g. two `anthropic/*` entries with different API keys), LiteLLM's router distributes requests across them. When one fails, two mechanisms handle failover:

1. **Retries** (`num_retries`): On failure, the router retries the request on a different deployment within the same model group.
2. **Cooldowns** (`allowed_fails` + `cooldown_time`): After enough consecutive failures, a deployment is temporarily removed from rotation.

### `allowed_fails` semantics (not obvious)

The cooldown check is `fails > allowed_fails`. This means:

| `allowed_fails` | Failures before cooldown | Notes |
|---|---|---|
| `0` | 1st failure triggers cooldown | **Too aggressive** — startup probes can cool down healthy deployments |
| `1` | 2nd failure triggers cooldown | **Recommended** — tolerates one transient error |
| `3` (old default) | 4th failure triggers cooldown | Too many wasted requests before the dead key is removed |

### Startup health probes (the hidden gotcha)

At startup, LiteLLM runs internal health probes against all deployments. These probes are real API calls (tagged `litellm-internal-health-check`) that:
- Test model availability by calling small requests against various model names
- Count toward the `allowed_fails` failure counter
- Can mark deployments as unhealthy if they fail

**Impact**: If a deployment has an invalid or exhausted key, the startup probe fails and that deployment is immediately cooled down. If `allowed_fails=0` or a transient rate limit hits the healthy key during the probe burst, ALL deployments can be cooled down simultaneously, leaving zero healthy endpoints.

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
  num_retries: 3
  allowed_fails: 1
  cooldown_time: 600  # 10 min
```

### Diagnosing failover issues

```bash
# Check deployment health
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/health | python3 -m json.tool

# Count successes vs failures in recent requests
curl -s -H "Authorization: Bearer $KEY" "http://localhost:4000/spend/logs?limit=200" | python3 -c "
import sys, json
from collections import Counter
data = json.load(sys.stdin)
by_status = Counter(r.get('status','?') for r in data)
print('Request outcomes:', dict(by_status))
by_model_id = Counter(
    r.get('metadata',{}).get('model_id','?')
    for r in data if r.get('status') == 'failure'
)
if by_model_id:
    print('Failures by deployment:', dict(by_model_id))
"

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
