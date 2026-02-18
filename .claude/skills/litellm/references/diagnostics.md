# LiteLLM Diagnostics Reference

All commands assume SSH to pyncher-server. Use `ssh pyncher-server '<command>'` when running remotely.

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
| `BaseLLMException` + "OAuth token has expired" | Token expired between refreshes | Transient; retries handle it. If persistent, run `claude setup-token` on server |
| `BaseLLMException` + "x-api-key header is required" | Auth header missing during key rotation | Transient; resolves on retry |
| `BaseLLMException` + "invalid x-api-key" | Stale key after rotation | Transient; resolves after gateway restart |
