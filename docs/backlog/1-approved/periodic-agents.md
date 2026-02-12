# Periodic Agents

Background agents that run on schedules to maintain and improve pynchy and managed repos.

## Context

These should run as subagents in the host. Need a claude rule with guidelines for authoring them — recommended patterns such as a rotating log of which files have been processed, so they are less inefficient.

## Proposed Agents

- **Security sweep:** Scan the repo for security concerns.
- **Upstream feature check:** Check upstream repos for features to copy.
- **OpenClaw feature check:** Check [OpenClaw](https://github.com/openclaw/openclaw) for ideas and suggestions.
- **Code simplifier:** Clean up code quality, think about how to simplify. Keep a rotating log of which files have been audited so future instances don't repeat work. Should detect janky code and fix it eagerly. Could extend to a list of repos pynchy manages — e.g. all plugin repos it has authored. Should be configurable so a repo doesn't get scanned again unless there's been a code edit since the last simplification check.
- **Skill reformatter:** Reformat all skills to follow the progressive disclosure principle.
- **Rule reviewer:** Review skills/rules in individual projects, decide if any should be promoted to global rules that all agents use. Gated behind Deputy agent + human review.
- **SDK feature reviewer:** Review new features in the Claude SDK, integrate them if they make sense.
- **Recovery agent:** Check logs for errors/warnings from the day, patch them up and re-deploy. Needs Tailscale for remote commands. Should be runnable on external machines in case the deployment becomes unhealthy.
- **Health endpoint:** Accessible over Tailscale. Reports: which periodic agents ran in the last 24 hours, whether the deployment is up, whether there have been edits to the git repo, how many convos since the last deploy, how many convos in the past day.

## Self-Improvement Workflow

When pynchy has an idea for self-improvement (new feature, etc.):
1. Check in with the human — give the pitch
2. If signed off, create a plan.md in the backlog
3. Periodic implementer agent reviews the backlog plans and decides when to spawn workers
4. Ideally runs at odd hours to use up subscription usage limits in different windows — e.g. check at end of week if usage allowance will be forfeited, and if so, spawn workers
5. Future: if there are multiple subscriptions on different accounts, load balance work across them to maximize value

## Related: LLM Usage Limit MCP

An MCP that checks usage limits for different LLM providers. If close to the end of a limit reset cycle, go aggressive with token burn but make sure to stop after the reset time.

## Plan
TBD
