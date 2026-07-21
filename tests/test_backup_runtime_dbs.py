from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess  # noqa: S404 - tests execute the repository's fixed backup script.
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "backup_runtime_dbs.sh"
BASH = Path("/bin/bash")
DATABASES = ("messages.db", "memories.db", "neonize.db", "temporal.db")


def _create_database(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _fake_launchctl(fake_bin: Path) -> Path:
    calls = fake_bin / "launchctl.calls"
    executable = fake_bin / "launchctl"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$FAKE_LAUNCHCTL_CALLS"\n'
        'if [[ "$1" == print ]]; then exit 0; fi\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return calls


def _run_backup(
    tmp_path: Path,
    *,
    sqlite3_executable: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    fake_bin = tmp_path / "bin"
    home = tmp_path / "home"
    plist = home / "Library" / "LaunchAgents" / "com.pynchy.temporal.plist"
    data_dir.mkdir()
    fake_bin.mkdir()
    plist.parent.mkdir(parents=True)
    plist.write_text("plist", encoding="utf-8")
    for name in DATABASES:
        _create_database(data_dir / name, name)
    calls = _fake_launchctl(fake_bin)
    if sqlite3_executable is not None:
        (fake_bin / "sqlite3").symlink_to(sqlite3_executable)

    environment = os.environ | {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_LAUNCHCTL_CALLS": str(calls),
        "PYNCHY_BACKUP_DIR": str(backup_dir),
        "PYNCHY_DATA_DIR": str(data_dir),
    }
    result = subprocess.run(  # noqa: S603 - fixed script path with isolated test inputs.
        [BASH, SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result, backup_dir, calls


def test_backup_quiesces_temporal_and_publishes_complete_snapshot(tmp_path: Path) -> None:
    result, backup_dir, calls = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    snapshots = [path for path in backup_dir.iterdir() if not path.name.startswith(".partial-")]
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert {path.name for path in snapshot.iterdir()} == {*DATABASES, "SHA256SUMS"}
    with sqlite3.connect(snapshot / "temporal.db") as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("temporal.db",)
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"print gui/{os.getuid()}/com.pynchy.temporal",
        f"bootout gui/{os.getuid()}/com.pynchy.temporal",
        (
            f"bootstrap gui/{os.getuid()} "
            f"{tmp_path}/home/Library/LaunchAgents/com.pynchy.temporal.plist"
        ),
    ]


def test_backup_restarts_temporal_when_snapshot_fails(tmp_path: Path) -> None:
    fake_sqlite = tmp_path / "failing-sqlite3"
    real_sqlite = shutil.which("sqlite3")
    assert real_sqlite is not None
    fake_sqlite.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == */temporal.db ]]; then exit 23; fi\n'
        f'exec "{real_sqlite}" "$@"\n',
        encoding="utf-8",
    )
    fake_sqlite.chmod(0o755)

    result, backup_dir, calls = _run_backup(tmp_path, sqlite3_executable=fake_sqlite)

    assert result.returncode == 23
    assert "Incomplete backup retained" in result.stderr
    assert not [path for path in backup_dir.iterdir() if not path.name.startswith(".partial-")]
    assert calls.read_text(encoding="utf-8").splitlines()[-1] == (
        f"bootstrap gui/{os.getuid()} "
        f"{tmp_path}/home/Library/LaunchAgents/com.pynchy.temporal.plist"
    )
