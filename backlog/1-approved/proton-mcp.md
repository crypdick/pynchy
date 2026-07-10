# Proton Mail (via pm-cli) + Proton Calendar (via Playwright) — Design

## Problem

Access Proton Mail and Proton Calendar from pynchy agents. Proton has no
public REST API and no CalDAV. Browser automation was historically the only
option. For mail specifically, `pm-cli` (Go CLI over Proton Bridge
IMAP/SMTP) now provides a simpler path.

## Approach

**Hybrid:**
- **Mail** — host-side `pm-cli` binary, wrapped by a pynchy service-handler
  plugin that exposes MCP tools to agents. No browser automation, no chrome
  profile, no login flow code.
- **Calendar** — browser automation via the existing Playwright browser MCP
  server. Unchanged from the prior iteration of this design.

The split is intentional: Proton Mail has a stable CLI surface; Proton
Calendar does not. Using `pm-cli` eliminates most of the complexity for
the mail path. Calendar still needs browser automation until Proton ships
an API or a CLI wrapper emerges.

## Security Caveat (load-bearing)

`pm-cli` (commit `7c41145a`, audited 2026-04-23 — see
`~/Documents/obsidian/wiki/systems/machines/dream-machine/pm-cli.md`) has
unpatched CRLF header-injection bugs: user-controlled values flow into
`fmt.Fprintf(w, "Header: %s\r\n", value)` without escaping. A
prompt-injection attack that tricks the agent into calling `mail_send`
with a CRLF-laced parameter would land injected headers directly in
outbound SMTP.

**Pynchy-side mitigation:** every `pm-cli` invocation is wrapped by a
CRLF sanitizer. `\r` or `\n` in any header-valued parameter raises, the
subprocess never runs. Defeats the amplifier at the pynchy boundary,
independent of upstream patching.

When upstream tags a fix, the sanitizer remains (defense in depth) or
can be removed at that point.

**Separate action (not in scope):** send the draft disclosure to
upstream maintainer `bscott`. Per obsidian note, draft exists at
`/tmp/pm-cli-disclosure.txt` (volatile — move before reboot if not sent).

## Mail Architecture

```
Host
  ├─ Proton Bridge (127.0.0.1:1143 IMAP, 127.0.0.1:1025 SMTP)
  ├─ libsecret keyring (Bridge creds under service "pm-cli")
  ├─ ~/go/bin/pm-cli (reads creds from keyring automatically)
  │
  └─ pynchy process
       └─ plugins/integrations/proton.py
           ├─ _sanitize_header(value) → raises on CRLF
           └─ service handlers:
                 proton_mail_list, _read, _send, _reply, _search, _archive

Agent container
  ├─ proton-mail skill (SKILL.md) — teaches tool usage
  └─ calls mcp__pynchy__proton_mail_* via the pynchy MCP server
```

Key point: pm-cli, Proton Bridge, and libsecret all live on the **host**.
The agent container never touches them directly. The handler runs in the
pynchy host process; the agent calls it via the existing
`mcp__pynchy__*` tool surface.

## Mail Plugin

**File:** `src/pynchy/plugins/integrations/proton.py`

```python
import subprocess
from collections.abc import Callable
from pathlib import Path

from pynchy.plugin.hookspecs import hookimpl

_PM_CLI = Path.home() / "go" / "bin" / "pm-cli"


def _sanitize_header(value: str) -> str:
    """Reject CRLF in header parameters.

    pm-cli commit 7c41145a emits headers via unescaped fmt.Fprintf, so a
    CRLF in a user-controlled value injects arbitrary SMTP headers. This
    guard makes that class of attack fail closed at the pynchy boundary.
    """
    if "\r" in value or "\n" in value:
        raise ValueError(f"CRLF in header parameter rejected: {value!r}")
    return value


class ProtonMailPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Callable]:
        return {
            "proton_mail_list": self.list_messages,
            "proton_mail_read": self.read_message,
            "proton_mail_send": self.send_message,
            "proton_mail_reply": self.reply_message,
            "proton_mail_search": self.search,
            "proton_mail_archive": self.archive,
        }

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
    ) -> dict:
        args = [
            str(_PM_CLI), "mail", "send",
            "--to", _sanitize_header(to),
            "--subject", _sanitize_header(subject),
        ]
        if cc:
            args.extend(["--cc", _sanitize_header(cc)])
        args.extend(["--body", body])  # body is MIME-encoded, not header — no sanitization
        result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=30)
        return {"message_id": result.stdout.strip()}

    # Similar wrappers for list/read/reply/search/archive.
    # Every header-typed argument flows through _sanitize_header.

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return ["src/pynchy/agent/skills/proton-mail/"]
```

**Sanitization scope:** header-valued fields only (`to`, `cc`, `bcc`,
`subject`, `from`, `reply_to`, `in_reply_to`). Body content is never
sanitized — it's MIME-encoded, legitimate content contains newlines.

**Registration:** add to `_BUILTIN_PLUGIN_SPECS` in
`src/pynchy/plugin/__init__.py`.

## Mail Skill

**File:** `src/pynchy/agent/skills/proton-mail/SKILL.md`

Teaches the agent the six tools with signatures, examples, and failure
modes. Documents that CRLF in header fields raises — so the agent should
stick to normal email address formatting and single-line subjects.

## Calendar Path (unchanged from prior design)

Calendar retains the browser-automation design:

- `browser` MCP instance `browser.proton_calendar` with a dedicated
  chrome profile at `data/chrome-profiles/proton/`
- `proton-calendar` skill teaching the agent to drive `calendar.proton.me`
- `proton_login` service handler for session refresh (needed only for
  calendar now that mail uses pm-cli)
- TOTP generation if `PROTON_2FA_SECRET` is set in `.env`

The two pre-existing "Implementation Gaps" still apply to calendar:
1. `--user-data-dir` injection for script-type MCP servers (extend
   `_merged_mcp_servers`)
2. Credential forwarding for the login handler

Mail bypasses both.

## Config

Host `config.toml`:
```toml
# Mail — no config needed beyond plugin registration.
# proton_mail_* tools are exposed via the builtin pynchy MCP server.

# Calendar — browser MCP instance
chrome_profiles = ["proton"]

[mcp_servers.browser.proton_calendar]
chrome_profile = "proton"

[sandbox.work]
mcp_servers = ["browser.proton_calendar"]
# proton_mail_* inherited automatically from pynchy MCP

[sandbox.notifications]
# mail only — no browser instance needed for this sandbox
mcp_servers = []
```

Host `.env`:
```
# Only for calendar (browser login). Mail uses Bridge keyring.
PROTON_USERNAME=user@proton.me
PROTON_PASSWORD=...
PROTON_2FA_SECRET=...   # optional, base32 TOTP
```

## Prerequisites

**Host setup (already done, per obsidian note):**
- Proton Bridge installed and listening on `127.0.0.1:1143` /
  `127.0.0.1:1025`
- `pm-cli` at `~/go/bin/pm-cli` (installed via
  `go install github.com/bscott/pm-cli/cmd/pm-cli@latest`)
- Bridge credentials stored in libsecret under service name `pm-cli`
- libsecret unlocks automatically on login (verify on mac-mini —
  headless boot needs the keyring unlocked or pm-cli won't see creds)

**Pynchy work:**
1. Implement `proton.py` plugin: sanitizer + six service handlers
2. Write `proton-mail/SKILL.md`
3. Register in `_BUILTIN_PLUGIN_SPECS`
4. Unit-test the sanitizer against a CRLF corpus
5. Calendar work tracked separately (same pre-reqs as original plan)

## Risks

| Risk | Mitigation |
|------|-----------|
| pm-cli header injection (audited bug) | `_sanitize_header` on every header param; fails closed |
| Prompt injection via inbound mail content | Inherent to any mail integration; out of scope for this design |
| Proton Bridge process unavailable | pm-cli surfaces connection error; handler returns error to agent |
| libsecret locked on headless boot | Document keyring auto-unlock requirement for mac-mini deployment |
| pm-cli binary path varies per machine | Env var override; default `~/go/bin/pm-cli` |
| Calendar path gaps still unresolved | Tracked as original plan's implementation gaps |
| Upstream pm-cli releases incompatible changes | Pin version in install docs; sanitizer is version-independent |

## Open Questions

1. `proton_mail_read` return format — HTML, plaintext, or both? pm-cli
   supports both; default to plaintext with optional HTML retrieval.
2. Attachments — read-only initially, or support send+read?
3. Rate limiting — Proton/Bridge enforces quotas; should pynchy surface
   quota state to the agent or rely on error-path surfacing?
4. Multiple Proton accounts — pm-cli supports per-email keyring entries.
   Defer multi-account until the broader "multiple accounts" backlog
   item lands.
