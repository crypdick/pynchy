# Small Improvements

Smaller tasks that don't need their own plan file.

- [ ] **Dossier logging audit:** Assess dossier for logging. We want really dense logging for debugging purposes.
- [x] **Ruff auto-fix hooks:** Add a rule to simplify future agent run's workflow — e.g. using ruff's built-in auto formatting fixer instead of manually fixing things. This could be enforced by hooks: detect if the agent ever runs ruff without the auto-fix flag and throw an error telling it to always use the auto linter. Should probably be done with Claude's built-in hook system.
- [ ] **Check slack-tools migration:** Check whether any features from slack-tools should be migrated to pynchy.
- [x] **Distinct system messages:** Meta/system messages addressed to the user (not the agent) should be visually distinct from agent responses — e.g. a different format or prefix — so the user can tell whether a message originated from the system harness vs the agent. The agent should not see these messages in its context.
- [x] **External pull & restart:** HTTP endpoint bound to the Tailscale interface only (100.x.x.x) that instructs pynchy to `git pull` and restart itself. Ensures remote deploys are only reachable from the tailnet, not the public internet.
