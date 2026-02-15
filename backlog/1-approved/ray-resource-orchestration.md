# Ray Distributed Resource Orchestration

Thin integration with [Ray](https://docs.ray.io/) as a resource orchestrator for container agents.

## Motivation

Currently container concurrency is managed with a simple semaphore. Ray replaces this with resource-aware scheduling.

## Benefits

1. **Resource-aware scaling** — Scale container count based on actual available CPU/memory, not a hardcoded limit.
2. **Blocking task queues** — Tasks block and wait until resources free up, handled natively by Ray's scheduler.
3. **Multi-node scaling** — Distribute agents across multiple machines with no code changes.
4. **Custom hardware** — Route tasks to nodes with specific capabilities (e.g., GPU for vision/embedding workloads).

## Scope

Thin integration — use Ray as a resource orchestrator only. The existing container runtime (Apple Container / Docker) still builds and runs containers; Ray manages *when* and *where* they run.

## Open Questions

- Replace `group_queue.py` semaphore entirely, or keep it as a local fallback?
- Ray head node lifecycle — embed in the pynchy process or run as a separate service?
- Resource labeling scheme for heterogeneous nodes (GPU, high-memory, etc.)
