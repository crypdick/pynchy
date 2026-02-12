*This file is for tracking potential future projects that are out of scope for now.*


# God mode container
can self-author pynchy, and restart the service.
the goal is for this contaienr to allow me to add new features to pynchy (modify its codebase), have it restart itself, and after restart restart the god container with its context so that it can continue 
working on the feature where it left off. I want to be able to do this over WhatsApp in a special channel.

# Misc

- [ ] assess dossier for logging
- [ ] check whether any features from slack-tools should be migrated
- [ ] make sure all typescript stuff has been migrated. add a 'differences from typescript implementation' for substantial differences
- [x] make the container runtime detection more robust for Linux vs macOS. Currently hardcoded to Apple Container (`container` CLI) — should detect platform and support Docker on Linux (different CLI args, no `container system start`, different orphan cleanup)
  > Done: `src/pynchy/runtime.py` — frozen `ContainerRuntime` dataclass with lazy singleton. Detects via `CONTAINER_RUNTIME` env var → platform → `shutil.which()`. Apple Container uses `system status`/`start` + array JSON listing. Docker uses `docker info` + newline-delimited JSON. `container_runner.py` and `app.py` delegate to runtime. `build.sh` has matching shell detection.
- [ ] make the repo less dependent on claude sdk. define a generic interface for agents and let people install plugins for other LLMs. for example, open-code: https://opencode.ai/docs/


# Periodic agents
- [ ] sweep through the repo, check for security concerns
- [ ] check the upstream repo; are there features to copy?
- [ ] check openclaw; are there features to copy? https://github.com/openclaw/openclaw make suggestions.
- [ ] code simplifier: clean up code quality, think about how to simplify
- [ ] review skills / rules in the individual projects. decide whether any of them should be promoted to global rules that all agents use. gate this behind the Deputy agent + human review


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
- 
