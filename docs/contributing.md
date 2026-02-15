# Contributing

## Source Code Changes

**Accepted:** Bug fixes, security fixes, simplifications, reducing code.

**Not accepted:** New features, integrations, or enhancements to the base codebase.

Everything else should be contributed as **plugins**.

## Why Plugins?

Pynchy stays minimal by design. If you want to add Telegram support, don't create a PR that adds Telegram alongside WhatsApp. Instead, contribute a plugin.

This keeps the base system small and lets every user customize their installation without inheriting features they don't want.

## Writing Plugins

See the [Plugin Authoring Guide](plugins/index.md) for how to create, package, and distribute plugins.

## Development

See `.claude/development.md` in the project root for running commands, writing tests, and linting.
