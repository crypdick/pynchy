*This file is for tracking potential future projects that are out of scope for now.*


# God mode container
can self-author pynchy, and restart the service.
the goal is for this contaienr to allow me to add new features to pynchy (modify its codebase), have it restart itself, and after restart restart the god container with its context so that it can continue 
working on the feature where it left off. I want to be able to do this over WhatsApp in a special channel.

# Misc

- [ ] ability to reset a context in whatsapp. i should be able to type 'reset' which starts a fresh session, and also makes it so that future sessions don't automatically see the convo history from before the reset.
- [ ] assess dossier for logging. we want really dense logging for debugging purposes.
- [ ] check whether any features from slack-tools should be migrated
- [x] make sure all typescript stuff has been migrated. add a 'differences from typescript implementation' for substantial differences
  > Done: Full audit confirmed 100% test parity (105/105 TS tests ported) + ~95 new Python-only tests. All modules faithfully ported. TS source deleted.
- [ ] port x-integration skill from TypeScript to Python plugins (archived at `docs/archive/x-integration-skill-ts/`)
- [x] make the container runtime detection more robust for Linux vs macOS. Currently hardcoded to Apple Container (`container` CLI) — should detect platform and support Docker on Linux (different CLI args, no `container system start`, different orphan cleanup)
  > Done: `src/pynchy/runtime.py` — frozen `ContainerRuntime` dataclass with lazy singleton. Detects via `CONTAINER_RUNTIME` env var → platform → `shutil.which()`. Apple Container uses `system status`/`start` + array JSON listing. Docker uses `docker info` + newline-delimited JSON. `container_runner.py` and `app.py` delegate to runtime. `build.sh` has matching shell detection.
- [ ] make the repo less dependent on claude sdk. define a generic interface for agents and let people install plugins for other LLMs. for example, open-code: https://opencode.ai/docs/
- [ ] integration with tailscale: have pynchy setup its own tailscale service. this enables me to use claude code web to also author the pynchy repo, and remotely trigger a deploy + health check. it could also enable CI/CD to manage the service. this should be part of the deploy process. for now, let's assume that there is only one pynchy deployment at a time. the pynchy deployment would then be interactable via cli cmds, not just via whatsapp. maybe one ux is that when we run 'uv run pynchy' it actually runs it remotely over tailscale..? something to consider. or maybe that should be its own separate uv run pynchy-remote entrypoint..?
- [ ] add a rule to simplify future agent run's workflow. for example, using ruff's built in auto formatting fixer instead of manually fixing things. this could even be enforced by hooks, eg detect if the agent ever runs ruff without the auto fix flag and throw an error telling it to always use the auto linter. actually, this should probably be done with claude's built in hook system.


# Periodic agents
- [ ] these periodic agents should run as subagents in the host, i think.
- [ ] sweep through the repo, check for security concerns
- [ ] check the upstream repo; are there features to copy?
- [ ] check openclaw; are there features to copy? https://github.com/openclaw/openclaw make suggestions.
- [ ] code simplifier: clean up code quality, think about how to simplify. this would ideally keep a rotating log of which files have been audited so that future instances don't repeat the work. we should detect janky code and fix it eagerly.
- [ ] claude rule with guidelines for authoring periodic agents. it should have recommended patterns, such as the rotating log of which files have been processed, so that they are less inefficient
- [ ] we should reformat all of our skills such that it follows the progressive disclosure principle.
- [ ] it could be nice to extend some of these agents (e.g. the code simplifier) to a list of repos that pynchy manages. for example, we might have pynchy managing all the plugin repos it has authored; the periodic agent might run on a slow cadence to improve these skills. it should be able to configure this scanning behavior, e.g. enable a setting so that a repo does not get scanned for code simplifications again unless there has been a code edit since the last simpliciation check.
- [ ] review skills / rules in the individual projects. decide whether any of them should be promoted to global rules that all agents use. gate this behind the Deputy agent + human review
- [ ] review new features in the claude sdk. integrate them into the repo if they make sense.
- [ ] whenever pynchy has an idea for self-improvement (new feature, etc), it should check in with me. give me the pitch. if I sign off, it should create a plan.md doc. there should be a backlog/ folder with different status folders: 'someday/maybe', 'approved-planning', 'denied', 'approved-in-progress', 'completed'. this TODO.md should be moved into backlog/TODO.md. this todo.md would then link out to it's specific plan.md file. as things are implemented, this todo.md should be kept clean so that it is just a thin tracking doc. then, we can have our periodic implementer agent review this todo and the plans and decide when to spawn workers. this would ideally run at odd hours to use up my claude subscription usage limits in different windows. like, it should check at the end of the week if I will be forfeiting my usage allowance, and if so, spawn workers to do the implementations. even better, in the future if I have multiple subscriptions on different accounts, it can load balance work across the different accounts to maximize the value i get out of them. 
- [ ] following up on above, there should be a mcp which checks my usage limits for different llm providers. if it is close to the end of a limit reset cycle, it should go wild with the token burn but make sure it stops after the reset time.
- [ ] recovery agent. checks for errors or warnings in the logs from that day. if it finds any, patches them up and re-deploys. tailscale would be helpful for this so that it may issue remote commands, I suppose? It may be best for this to be runable on external machines in case the deployment becomes unhealthy
- [ ] a health endpoint accessible over tailscale. this reports which periodic agents ran in the last 24 hours, whether the deployment is up, whether there have been any edits to the git repo, how many convos since the last deploy, how many convos in the past day, etc.


# Project ideas
- calendar manager mcp
- fun plans mcp
- friends hangout mcp
- health / wellness / exercise mcp
- ability to call me
- ability to make voice announcements to my home speakers
- cloudflare mcp
- host has a skill for creating new repos. it creates a new github app password granting the workspace access to just that repo.
- aws / boto3 + terraform mcp
- main agent skill: create new pynchy plugin. 1) create new plugin repo & app password granting the workspace access to just that repo. 2) create a new container. clone the new repo into the container. init the repo using a templater template, 3) create a new whatsapp group for the plugin, 4) pynchy sends a welcome message to the group with a link to the plugin repo, asks user to explain what they want the plugin to do. 5) pynchy goes into plan mode, comes up with a plan for user to approve, 6) pynchy implmenets the plugin. when done, user has to go to the god container and ask it to install the plugin and restart the service.
- voice to text support, tts support.
- ability to run local llms for simple jobs. probably blocked on the plugins for using different providers.

