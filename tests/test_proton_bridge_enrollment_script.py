from __future__ import annotations

import os
import stat
import subprocess  # noqa: S404 - test executes the fixed repository script with fake tools.
from pathlib import Path

SCRIPT = Path("deploy/k3s/proton-bridge-enroll.sh")


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_enrollment_keeps_password_out_of_commands_and_applies_secret(tmp_path: Path) -> None:
    personalization = tmp_path / "personalization"
    overlay = personalization / "ops/k3s/proton-bridge-secret"
    overlay.mkdir(parents=True)
    (personalization / ".gitignore").write_text(
        "/ops/k3s/proton-bridge-secret/proton-bridge.env\n",
        encoding="utf-8",
    )
    (overlay / "kustomization.yaml").write_text("kind: Kustomization\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "init", "--quiet"],
        cwd=personalization,
        check=True,
        capture_output=True,
        text=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "k3s.calls"
    _executable(
        fake_bin / "sudo",
        '#!/bin/sh\nexec "$@"\n',
    )
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "stat", "#!/bin/sh\nprintf '3000\\n'\n")
    _executable(fake_bin / "chown", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "script",
        '#!/bin/sh\nshift\nPYNCHY_BATCH_BRIDGE=1 /bin/sh -c "$1"\n',
    )
    _executable(
        fake_bin / "k3s",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PYNCHY_K3S_CALLS"\n'
        'case " $* " in\n'
        '  *" get deployment pynchy "*) printf "pynchy proton-bridge litellm" ;;\n'
        '  *" exec -it deployment/pynchy -c proton-bridge "*)\n'
        '    if [ "${PYNCHY_BATCH_BRIDGE:-}" = 1 ]; then\n'
        '      printf "Password:  %s\\r\\n" "$PYNCHY_BRIDGE_FIXTURE"\n'
        "    fi ;;\n"
        "esac\n",
    )

    bridge_value = "bridge-fixture-value"
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYNCHY_BRIDGE_FIXTURE": bridge_value,
        "PYNCHY_K3S_CALLS": str(calls),
        "PYNCHY_PERSONALIZATION_ROOT": str(personalization),
        "PYNCHY_PROTON_BRIDGE_SECRET_OVERLAY": str(overlay),
    }
    result = subprocess.run(  # noqa: S603 - fixed script path with isolated fake commands.
        ["/bin/sh", SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    secret_file = overlay / "proton-bridge.env"
    assert secret_file.read_text(encoding="utf-8") == f"password={bridge_value}\n"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    command_log = calls.read_text(encoding="utf-8")
    assert command_log.count("exec -it deployment/pynchy -c proton-bridge") == 2
    assert f"apply -k {overlay}" in command_log
    assert "create_proton_mail_client().list_mailboxes()" in command_log
    assert bridge_value not in command_log + result.stdout + result.stderr
    assert "Proton Bridge enrollment complete." in result.stdout

    secret_file.unlink()
    calls.write_text("", encoding="utf-8")
    resumed = subprocess.run(  # noqa: S603 - fixed script path with isolated fake commands.
        ["/bin/sh", SCRIPT, "--resume"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert secret_file.read_text(encoding="utf-8") == f"password={bridge_value}\n"
    resume_log = calls.read_text(encoding="utf-8")
    assert resume_log.count("exec -it deployment/pynchy -c proton-bridge") == 1
    assert bridge_value not in resume_log + resumed.stdout + resumed.stderr
