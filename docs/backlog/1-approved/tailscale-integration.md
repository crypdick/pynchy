# Tailscale Integration

Have pynchy set up its own Tailscale service for remote access and management.

## Context

Enables:
- Using Claude Code web to author the pynchy repo remotely
- Triggering deploys + health checks remotely
- CI/CD managing the service
- CLI interaction, not just WhatsApp

Assumes a single pynchy deployment at a time. Open question: should `uv run pynchy` run it remotely over Tailscale, or should there be a separate `uv run pynchy-remote` entrypoint?

Part of the deploy process. Also a dependency for the recovery agent and health endpoint ideas.

## Plan
TBD
