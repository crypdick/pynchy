# Plugin Security Model

All plugin Python code runs on the host during discovery (`__init__`, `validate()`, category methods). Installing a plugin = trusting its code. Risk by category:

| Category | Sandbox level | Risk | Why |
|----------|--------------|------|-----|
| **Channel** | None — runs persistently in host process | **Highest** | Full filesystem, network, and runtime access for app lifetime |
| **Skill** | Partial — `skill_paths()` on host, content in container | **Medium** | Host method can read arbitrary paths or have side effects |
| **Hook** | Partial — class on host, hook code in container | **Medium** | Host-controlled module path; container code runs with `bypassPermissions` |
| **MCP** | Mostly sandboxed — spec on host, server in container (read-only mount) | **Lower** | Brief host execution; server isolated in container |

**Rule: only install plugins from authors you trust.** See `plugin/base.py` docstring for full details.
