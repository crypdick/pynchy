---
name: prefer-deploy-endpoint
enabled: true
event: bash
pattern: systemctl\s+(--user\s+)?(restart|stop|start)\s+pynchy|docker\s+(kill|stop|restart|rm)\s+pynchy
action: warn
---

You're manually managing the pynchy service or its containers. **This is almost never necessary.**

Pynchy self-manages — it auto-restarts when config files change (`config.toml`, `litellm_config.yaml`) and when new commits land on `main`. Just edit the file or push your commit, then wait ~30–90s.

If you need to trigger a deploy explicitly:
```bash
curl -s -X POST http://pynchy:8484/deploy
```

**Only use manual service/container commands when the service is unhealthy and needs fixing** (e.g. the deploy endpoint is unreachable, the service is stuck, or you need to debug a crash).
