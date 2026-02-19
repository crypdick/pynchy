#!/bin/bash
# PreToolUse hook: block git push/pull/rebase inside containers.
# Agents must use the sync_worktree_to_main MCP tool instead, which
# coordinates through the host to avoid divergence between worktrees.
#
# Mounted read-only at /workspace/scripts/guard_git.sh

input=$(cat)
command=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
print(data.get('tool_input', {}).get('command', ''))
" <<< "$input" 2>/dev/null)

if echo "$command" | grep -qP '\bgit\s+(push|pull|rebase)\b'; then
  cat <<'EOF'
{"decision":"block","reason":"Direct git push/pull/rebase is blocked. Use the sync_worktree_to_main tool instead — it coordinates with the host to publish your changes (either merging into main or opening a PR, depending on workspace policy). Commit your changes first, then call sync_worktree_to_main."}
EOF
  exit 0
fi

echo '{}'
