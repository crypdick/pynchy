# Plugin Scanner

Third-party plugins run on the host machine with full process-level access. A malicious plugin could exfiltrate secrets, execute arbitrary commands, or compromise the system during import. The plugin scanner mitigates this by running an LLM-based security audit on every new plugin revision before installation.

This page explains how the scanner works and how to configure it.

## How It Works

The scanner runs during the startup sequence. On every boot:

1. **Sync** — Plugin repos are cloned or updated (git fetch). No code is installed or imported yet.
2. **Categorize** — Each plugin is checked against a local verdict cache (SQLite, keyed by plugin name + git SHA):
   - *Trusted* (explicit `trusted = true` in config) → install immediately.
   - *Cached pass* → install immediately.
   - *Cached fail* → skip (blocked).
   - *No verdict* → needs scanning.
3. **Boot** — Pynchy starts normally with only the trusted/verified plugins loaded.
4. **Audit** — If any unscanned plugins exist, the scanner spawns one container agent per plugin. The agent inspects the source code for malicious patterns and returns a `PASS` or `FAIL` verdict.
5. **Restart** — If new plugins pass, they are installed and pynchy restarts. On the second boot they hit the cache and load in step 2.

User messages stay blocked until the audit completes — channels don't connect until after step 4.

## What the Scanner Looks For

The scanning agent receives the plugin source code (read-only) inside its container and uses Bash to explore it. Checks include:

| Severity | Pattern |
|----------|---------|
| **Critical** (auto-fail) | Network calls in module-level code / hooks, reading `~/.ssh` / credentials / `.env`, `subprocess` / `os.system` for arbitrary commands, `eval` / `exec` on external input, obfuscated payloads, monkey-patching stdlib |
| **Suspicious** (fail if combined) | Dynamic imports of unusual modules, broad filesystem traversal, risky dependencies, prompt injection in skill files |
| **Acceptable** | Network in channel plugins (their purpose), file I/O in workspace paths, well-known dependencies |

The cache stores the verdict and reasoning. A failed plugin stays blocked until its git SHA changes (i.e., the author pushes a fix).

## Configuration

Configure plugins in `config.toml`:

```toml
[plugins.my-channel]
repo = "someone/pynchy-plugin-channel"
ref = "main"

[plugins.my-trusted-plugin]
repo = "myorg/pynchy-plugin-internal"
ref = "main"
trusted = true  # skip scanning — use for your own plugins
```

| Field | Default | Description |
|-------|---------|-------------|
| `repo` | (required) | GitHub `owner/repo` |
| `ref` | `"main"` | Git branch or tag |
| `enabled` | `true` | Set `false` to disable without removing |
| `trusted` | `false` | Skip scanner — plugin is installed without audit |

Only mark plugins `trusted` if you control the source code. Everything else goes through the scanner.

## Verdict Cache

Verdicts live in `~/.config/pynchy/plugins/verifications.db` (SQLite). Each row keys on `(plugin_name, git_sha)`, so updating a plugin to a new commit triggers a re-scan automatically.

You can inspect the cache directly:

```bash
sqlite3 ~/.config/pynchy/plugins/verifications.db \
  "SELECT plugin_name, substr(git_sha, 1, 12), verdict, reasoning FROM plugin_verifications"
```

To force a re-scan of a plugin, delete its row:

```bash
sqlite3 ~/.config/pynchy/plugins/verifications.db \
  "DELETE FROM plugin_verifications WHERE plugin_name = 'my-plugin'"
```

## Limitations

- The scanner uses an LLM — it can miss novel attack vectors or false-positive on legitimate patterns. Treat it as a defense-in-depth layer, not a guarantee.
- Each scan spawns a full container agent, taking 30–120 seconds depending on plugin size and LLM latency.
- The restart adds one extra boot cycle when new plugins first install.
