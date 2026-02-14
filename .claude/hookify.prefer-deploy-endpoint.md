---
name: prefer-deploy-endpoint
enabled: true
event: bash
pattern: systemctl\s+(--user\s+)?(restart|stop|start)\s+pynchy|docker\s+(kill|stop|rm)\s+pynchy
action: warn
---

You're manually managing the pynchy service or its containers. **Use the deploy endpoint instead:**

```bash
curl -s -X POST http://nuc-server:8484/deploy
```

This handles graceful shutdown, git pull, and restart cleanly.

**Only use manual service commands when something is broken** (e.g. the deploy endpoint is unreachable, the service is in a bad state, or you need to inspect a stuck process).
