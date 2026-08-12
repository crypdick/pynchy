from __future__ import annotations

import os
import subprocess  # noqa: S404 - test executes the fixed Temporal launcher.
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_temporal.sh"


def test_launcher_enforces_retention(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    updated = tmp_path / "updated"
    temporal = tmp_path / "temporal"
    temporal.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "$1" = server ]; then\n'
        '  touch "$TEMPORAL_READY"\n'
        "  sleep 0.2\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$5" = health ]; then test -f "$TEMPORAL_READY"; exit; fi\n'
        'test "$5" = update\n'
        'printf \'%s\\n\' "$*" > "$TEMPORAL_UPDATED"\n',
        encoding="utf-8",
    )
    temporal.chmod(0o755)
    environment = os.environ | {
        "TEMPORAL_READY": str(ready),
        "TEMPORAL_UPDATED": str(updated),
    }

    subprocess.run(  # noqa: S603 - fixed launcher with isolated fake CLI.
        [SCRIPT, temporal], check=True, env=environment, timeout=5
    )

    assert updated.read_text(encoding="utf-8").endswith(
        "operator namespace update --namespace default --retention 192h\n"
    )
