---
type: "query"
date: "2026-07-26T22:40:32.495871+00:00"
question: "How should lifecycle, HTTP server, webhook recovery, Temporal scheduler startup, and deploy continuation ownership compose, and which module owns cleanup?"
contributor: "graphify"
outcome: "useful"
source_nodes: ["lifecycle.py", "http_server.py", "webhook_ingress.py", "task_scheduler.py", "startup_handler.py"]
---

# Q: How should lifecycle, HTTP server, webhook recovery, Temporal scheduler startup, and deploy continuation ownership compose, and which module owns cleanup?

## Answer

lifecycle.py is the composition root: claim the deploy continuation once, prepare and bind a gated HTTP server, resolve deploy metadata, explicitly recover webhook routes, start connections, start one Temporal owner and await actual runtime entry, finalize the claimed continuation, publish HTTP, then start IPC. Each helper cleans only failures before ownership transfer; lifecycle owns reverse-order cleanup after transfer and joins task cancellation before rollback.

## Outcome

- Signal: useful

## Source Nodes

- lifecycle.py
- http_server.py
- webhook_ingress.py
- task_scheduler.py
- startup_handler.py
