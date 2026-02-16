# Plugin Verifier Agent

## Overview

Automated security review for third-party plugins. Before a plugin is activated, a verifier agent spawns in an isolated container, clones the plugin source, audits it for malicious code and prompt injection, and records a pass/fail judgement. Future installs pin to the audited git SHA so the exact reviewed code is used.

## Problem

All plugin types execute code on the host during discovery (see Plugin Security Model in CLAUDE.md). A malicious plugin could:

- Exfiltrate secrets via channel plugin host access
- Inject prompt overrides via skill SKILL.md files
- Shadow built-in skills to alter agent behavior
- Execute arbitrary code during `__init__` / `validate()` / category methods

Currently, installing a plugin = trusting it unconditionally. There's no verification step.

## Design

### Verification Flow

```
User installs plugin (uv pip install)
        │
        ▼
discover_plugins() finds new unverified entry point
        │
        ▼
Verifier agent spawns in isolated container
  - Clones plugin repo at HEAD
  - Records git SHA
  - Audits source code for:
    • Prompt injection in SKILL.md / hook modules
    • Malicious Python (file exfil, network calls in __init__, monkey-patching)
    • Overly broad host access patterns
    • Suspicious dependencies
  - Returns PASS / FAIL with reasoning
        │
        ▼
Result stored in SQLite: plugin name, git SHA, datestamp, verdict, reasoning
        │
        ├── PASS → Plugin activated, pinned to audited SHA
        └── FAIL → Plugin blocked, user notified with reasoning
```

### Future Updates

When a user updates a plugin (new git SHA):

1. Pynchy detects SHA mismatch vs. last audited SHA
2. Plugin is **blocked** until re-verified
3. Verifier agent runs again on new SHA
4. New verdict replaces old one in DB

### Trust Bypass

Users can mark specific plugins as trusted, skipping verification:

- `pynchy plugin trust <name>` — marks plugin as permanently trusted
- Intended for plugins the user authored or trusts completely
- Trusted status stored in DB alongside plugin record
- Trusting a plugin logs a warning (so it's auditable)

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS plugin_verifications (
    plugin_name TEXT NOT NULL,
    git_remote TEXT,
    git_sha TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
    reasoning TEXT,
    trusted INTEGER DEFAULT 0,
    trusted_at TEXT,
    PRIMARY KEY (plugin_name, git_sha)
);
CREATE INDEX IF NOT EXISTS idx_plugin_name ON plugin_verifications(plugin_name);
```

### Discovery Integration

Modify `discover_plugins()` to check verification status:

```python
def discover_plugins() -> PluginRegistry:
    for ep in entry_points(group="pynchy.plugins"):
        # 1. Load entry point metadata (but don't instantiate yet)
        # 2. Check plugin_verifications table:
        #    - If trusted=1 → skip verification
        #    - If verdict='pass' AND sha matches installed → proceed
        #    - If verdict='fail' OR no record OR sha mismatch → block
        # 3. If blocked and auto-verify enabled → spawn verifier
        # 4. Only instantiate plugin class after verification passes
```

### Verifier Agent Design

The verifier runs as a pynchy container agent with a specialized system prompt:

**Input:**
- Git clone URL (from plugin's pyproject.toml or installed package metadata)
- Git SHA to audit
- Plugin category (channel/skill/mcp/hook) — determines risk profile

**Audit checklist:**
1. **Host-side code** (highest priority):
   - `__init__`, `validate()`, category methods
   - Network calls, file I/O, subprocess calls, os/sys manipulation
   - Dynamic imports, eval/exec, monkey-patching
2. **Skill content** (SKILL.md files):
   - Prompt injection patterns (instruction override, role hijacking)
   - References to sensitive paths or credentials
   - Attempts to disable safety features
3. **Hook modules**:
   - Arbitrary code execution in hook functions
   - Access to paths outside `/workspace/`
   - Attempts to modify agent behavior or system prompt
4. **MCP server code**:
   - Network calls to unexpected hosts
   - File writes outside expected paths
   - Credential harvesting patterns
5. **Dependencies**:
   - Known-malicious packages
   - Suspicious post-install scripts
   - Dependency confusion risk (private package names)

**Output:**
- Verdict: PASS or FAIL
- Reasoning: human-readable explanation
- Risk flags: list of specific concerns (even on PASS)

### Pinning to Audited SHA

When a plugin passes verification, pynchy records the git SHA. On subsequent startups:

1. Resolve installed plugin's current git SHA (from `.git/` or package metadata)
2. Compare against last audited SHA in DB
3. If mismatch → block plugin until re-verified
4. If match → activate normally

**Note:** For editable installs (`uv pip install -e`), the SHA is the working tree HEAD. For regular installs, we may need to store the package version + hash instead.

## Implementation Steps

1. Add `plugin_verifications` table to DB schema
2. Add verification check to `discover_plugins()`
3. Build verifier agent system prompt and container config
4. Add `pynchy plugin trust <name>` CLI command
5. Add SHA detection for installed plugins (editable vs. regular)
6. Add re-verification flow for updated plugins
7. Tests: verification flow, trust bypass, SHA mismatch detection

## Open Questions

- How to resolve git remote URL from an installed package? `importlib.metadata` has package URLs but not always a git remote.
- Should verification be async (non-blocking startup) or sync (block until verified)?
- Cost: each verification spawns a container + LLM call. Acceptable for install-time, but what about startup checks?
- Should PASS verdicts expire after N days, forcing periodic re-review?
- How to handle plugins installed from PyPI (no git SHA, only package version)?

## Dependencies

- Plugin discovery system (completed)
- Container runtime (completed)
- SQLite DB layer (completed)
