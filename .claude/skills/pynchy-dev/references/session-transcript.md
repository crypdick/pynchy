# Session Transcript Branching

When agent teams spawn subagent CLI processes, they write to the same session JSONL. On subsequent `query()` resumes, the CLI reads the JSONL but may pick a stale branch tip (from before the subagent activity), causing the agent's response to land on a branch the host never receives a `result` for.

**Current state — closed by construction, not by an anchor.** The TS original anchored each resume with `resumeSessionAt` (last assistant UUID); that primitive was lost in the Python port and has no CLI successor, so it is *not* the fix here. Instead, native Teams tools (`TeamCreate`/`TeamDelete`/`SendMessage`) are not allow-listed in either core, so nothing can branch the leader's transcript. `Task` sidechains stay allowed — they write off the main chain and resume safely. Reintroducing Teams requires per-teammate session isolation first. The diagnostics below still apply for detecting any branching that does slip in.

## Diagnostics

```bash
# Check for concurrent CLI processes in session debug logs
ls -la data/sessions/<group>/.claude/debug/
# Each .txt file = one CLI subprocess. Multiple files = concurrent queries.

# Check parentUuid branching in transcript
uv run python << 'PYTHON_CODE'
import json

lines = open('data/sessions/<group>/.claude/projects/-workspace-group/<session>.jsonl').read().strip().split('\n')
for i, line in enumerate(lines):
    try:
        d = json.loads(line)
        if d.get('type') == 'user' and d.get('message'):
            parent = d.get('parentUuid', 'ROOT')[:8]
            content = str(d['message'].get('content', ''))[:60]
            print(f'L{i+1} parent={parent} {content}')
    except:
        pass
PYTHON_CODE
```

A healthy session has a linear chain of parentUuids. Branching (multiple entries sharing the same parentUuid) indicates concurrent or mis-anchored resumes.
