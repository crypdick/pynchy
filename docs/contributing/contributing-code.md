# Contributing

## Source Code Changes

**Accepted:** Security fixes, bug fixes, and clear improvements to the base configuration. That's it.

**Not accepted:** New features, integrations, enhancements, OS compatibility, or hardware support added to the base codebase.

Everything else should be contributed as **plugins**. This keeps the base system minimal and lets every user customize their installation without inheriting features they don't want.

## Why Plugins?

Pynchy stays minimal by design. If you want to add Telegram support, don't create a PR that adds Telegram alongside WhatsApp. Instead, contribute a plugin.

See the [Plugin Authoring Guide](plugins/index.md) for how to create, package, and distribute plugins.

## Development

See the `development` skill (`.claude/skills/development/SKILL.md`) for running commands, writing tests, and linting.
