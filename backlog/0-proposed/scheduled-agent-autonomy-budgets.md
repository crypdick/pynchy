# Scheduled Agent Autonomy And Budgets

## Problem

Scheduled agents now record attempt metadata and pause after repeated failures, but Pynchy still lacks an explicit policy model for how autonomous a periodic agent may be or how much model spend/work it may consume before requiring operator review.

## Proposed Shape

- Add a scheduled-agent autonomy setting in workspace config, for example report-only, assisted, and unattended.
- Define what each level may do structurally: report-only sends findings, assisted may prepare changes but requires approval, unattended may sync work through the existing host-mediated path.
- Add a budget source for scheduled agents before implementing budget-exhaustion circuit breakers. Prefer a host-visible source such as LiteLLM spend data or a Pynchy-maintained per-run estimate, not a prompt-only instruction.
- Expose autonomy level and budget state in `/status` next to scheduled-task run health.

## Non-Goals

- Do not copy L1/L2/L3 prompt labels from loop-engineering without mapping them to Pynchy permissions.
- Do not enforce budget exhaustion until Pynchy has a trustworthy budget signal.
