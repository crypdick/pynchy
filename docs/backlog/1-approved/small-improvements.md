# Small Improvements

Smaller tasks that don't need their own plan file.

- [ ] **WhatsApp context reset:** Ability to type "reset" which starts a fresh session, and also makes it so that future sessions don't automatically see the convo history from before the reset.
- [ ] **Dossier logging audit:** Assess dossier for logging. We want really dense logging for debugging purposes.
- [ ] **Ruff auto-fix hooks:** Add a rule to simplify future agent run's workflow — e.g. using ruff's built-in auto formatting fixer instead of manually fixing things. This could be enforced by hooks: detect if the agent ever runs ruff without the auto-fix flag and throw an error telling it to always use the auto linter. Should probably be done with Claude's built-in hook system.
- [ ] **Check slack-tools migration:** Check whether any features from slack-tools should be migrated to pynchy.
