#!/bin/bash
set -e

# Restore ~/.claude.json from backup if missing.
# The ~/.claude/ directory is mounted from the host (persists across restarts),
# but ~/.claude.json sits outside that mount and is lost when the container
# restarts. Claude Code backs it up inside ~/.claude/backups/ during operation,
# so we restore the latest backup to prevent startup failures.
if [ ! -f "$HOME/.claude.json" ] && [ -d "$HOME/.claude/backups" ]; then
  latest=$(ls -t "$HOME/.claude/backups"/.claude.json.backup.* 2>/dev/null | head -1)
  if [ -n "$latest" ]; then
    cp "$latest" "$HOME/.claude.json"
  fi
fi

python -m agent_runner
