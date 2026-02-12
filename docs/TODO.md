*This file is for tracking potential future projects that are out of scope for now.*


# God mode container
can self-author pynchy, and restart the service.
the goal is for this contaienr to allow me to add new features to pynchy (modify its codebase), have it restart itself, and after restart restart the god container with its context so that it can continue 
working on the feature where it left off. I want to be able to do this over WhatsApp in a special channel.

# Misc

- [ ] assess dossier for logging
- [ ] check whether any features from slack-tools should be migrated
- [x] make sure all typescript stuff has been migrated. add a 'differences from typescript implementation' for substantial differences
  > Done: Full audit confirmed 100% test parity (105/105 TS tests ported) + ~95 new Python-only tests. All modules faithfully ported. TS source deleted.
- [ ] port x-integration skill from TypeScript to Python (archived at `docs/archive/x-integration-skill-ts/`)
- [x] make the container runtime detection more robust for Linux vs macOS. Currently hardcoded to Apple Container (`container` CLI) — should detect platform and support Docker on Linux (different CLI args, no `container system start`, different orphan cleanup)
  > Done: `src/pynchy/runtime.py` — frozen `ContainerRuntime` dataclass with lazy singleton. Detects via `CONTAINER_RUNTIME` env var → platform → `shutil.which()`. Apple Container uses `system status`/`start` + array JSON listing. Docker uses `docker info` + newline-delimited JSON. `container_runner.py` and `app.py` delegate to runtime. `build.sh` has matching shell detection.
- [ ] make the repo less dependent on claude sdk. define a generic interface for agents and let people install plugins for other LLMs. for example, open-code: https://opencode.ai/docs/
- [ ] integration with tailscale: have pynchy setup its own tailscale service. this enables me to use claude code web to also author the pynchy repo, and remotely trigger a deploy + health check. it could also enable CI/CD to manage the service. this should be part of the deploy process. for now, let's assume that there is only one pynchy deployment at a time. the pynchy deployment would then be interactable via cli cmds, not just via whatsapp. maybe one ux is that when we run 'uv run pynchy' it actually runs it remotely over tailscale..? something to consider. or maybe that should be its own separate uv run pynchy-remote entrypoint..?

# Periodic agents
- [ ] sweep through the repo, check for security concerns
- [ ] check the upstream repo; are there features to copy?
- [ ] check openclaw; are there features to copy? https://github.com/openclaw/openclaw make suggestions.
- [ ] code simplifier: clean up code quality, think about how to simplify
- [ ] review skills / rules in the individual projects. decide whether any of them should be promoted to global rules that all agents use. gate this behind the Deputy agent + human review
- [ ] review new features in the claude sdk. integrate them into the repo if they make sense.


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
