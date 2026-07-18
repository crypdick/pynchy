# Google Workspace integration via gog

## Goal

Expose selected Google Workspace capabilities through `gog` while preserving
Pynchy's host/container security boundary.

## Scope to evaluate

- Gmail search, read, draft, and send
- Contacts lookup
- Google Docs read/export and Google Sheets read/write
- Typed, host-side service handlers with least-privilege OAuth scopes

## Boundary

Do not expose an unrestricted `gog` shell tool or mount gog's credentials into
agent containers. Pynchy already owns Google Calendar and Drive integrations;
decide explicitly whether any gog overlap replaces those paths before exposing
it.
