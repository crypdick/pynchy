"""Behavioral coverage for the launchd-owned local Temporal launcher."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test runs the repository launcher with a fake Temporal CLI.
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_temporal.sh"


def _write_fake_temporal(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "$1 $2" = "server start-dev" ]; then\n'
        '  : > "$TEMPORAL_STARTED"\n'
        "  trap ': > \"$TEMPORAL_STOPPED\"; exit 0' TERM INT\n"
        "  while :; do sleep 1; done\n"
        "fi\n"
        'if [ "$1 $2 $3" = "operator cluster health" ]; then\n'
        '  test -f "$TEMPORAL_STARTED"\n'
        "  exit\n"
        "fi\n"
        'if [ "$1 $2 $3" = "operator namespace update" ]; then\n'
        '  printf "%s\\n" "$*" > "$TEMPORAL_UPDATE"\n'
        "  exit\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_launcher_updates_retention_then_stops_temporal_child(tmp_path: Path) -> None:
    temporal = tmp_path / "temporal"
    _write_fake_temporal(temporal)
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    update = tmp_path / "update"
    process = subprocess.Popen(  # noqa: S603 - fixed test script and temporary CLI.
        ["/bin/sh", SCRIPT, temporal],
        env=os.environ
        | {
            "TEMPORAL_STARTED": str(started),
            "TEMPORAL_STOPPED": str(stopped),
            "TEMPORAL_UPDATE": str(update),
        },
    )
    try:
        _wait_for(update)
        assert update.read_text(encoding="utf-8") == (
            "operator namespace update --address 127.0.0.1:7233 "
            "--namespace default --retention 192h\n"
        )
    finally:
        process.terminate()
        assert process.wait(timeout=5) == 0

    _wait_for(stopped)


def test_launchd_template_uses_launcher() -> None:
    template = (Path(__file__).parents[1] / "launchd" / "com.pynchy.temporal.plist").read_text(
        encoding="utf-8"
    )

    assert "$PYNCHY_PROJECT_ROOT/scripts/run_temporal.sh" in template
    assert "<key>ExitTimeOut</key>" in template
