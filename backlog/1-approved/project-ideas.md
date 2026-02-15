# Project Ideas

Standalone project/integration ideas. Each could become its own backlog item when scoped.

- Calendar manager MCP
- Fun plans MCP
- Friends hangout MCP
- Health / wellness / exercise MCP
- Ability to call me (voice)
- Voice announcements to home speakers
- Cloudflare MCP
- Host skill for creating new repos — creates a new GitHub app password granting the workspace access to just that repo
- AWS / boto3 + Terraform MCP
- Voice-to-text and TTS support
- Run local LLMs for simple jobs (probably blocked on provider-agnostic agents)
- Main agent skill: create new pynchy plugin — 1) create new plugin repo & app password granting workspace access to just that repo, 2) create a new container, clone the new repo into the container, init the repo using a templater template, 3) create a new WhatsApp group for the plugin, 4) pynchy sends a welcome message to the group with a link to the plugin repo, asks user to explain what they want the plugin to do, 5) pynchy goes into plan mode, comes up with a plan for user to approve, 6) pynchy implements the plugin. When done, user goes to the god container and asks it to install the plugin and restart the service.
