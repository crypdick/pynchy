"""Regression tests for production agent-image build cleanup."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test invokes a repository-owned script with a controlled environment.
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildScriptRun:
    result: subprocess.CompletedProcess[str]
    runtime_calls: list[str]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_build_script(
    tmp_path: Path,
    *,
    fail_build: bool = False,
    fail_prune_call: int | None = None,
    runtime: str = "docker",
) -> BuildScriptRun:
    project_root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runtime_binary = "container" if runtime == "apple" else runtime
    runtime_log = tmp_path / "runtime.log"
    prune_count = tmp_path / "prune-count"
    _write_executable(
        fake_bin / runtime_binary,
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PYNCHY_TEST_RUNTIME_LOG"\n'
        'if [ "$PYNCHY_TEST_FAIL_BUILD" = "1" ]; then\n'
        '    case "$*" in build\\ *) exit 1 ;; esac\n'
        "fi\n"
        'if [ "$*" = "image prune -f" ]; then\n'
        '    count="$(cat "$PYNCHY_TEST_PRUNE_COUNT" 2>/dev/null || printf 0)"\n'
        '    count="$((count + 1))"\n'
        '    printf "%s" "$count" > "$PYNCHY_TEST_PRUNE_COUNT"\n'
        '    if [ "$count" = "$PYNCHY_TEST_FAIL_PRUNE_CALL" ]; then exit 1; fi\n'
        "fi\n"
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "uv",
        "#!/bin/sh\n"
        'if [ "$1" = "run" ] && [ "$2" = "python" ]; then\n'
        "    shift 2\n"
        '    exec "$PYNCHY_TEST_PYTHON" "$@"\n'
        "fi\n"
        "exit 0\n",
    )
    env = {
        **os.environ,
        "CONTAINER_RUNTIME": runtime,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYNCHY_TEST_FAIL_PRUNE_CALL": str(fail_prune_call or 0),
        "PYNCHY_TEST_FAIL_BUILD": "1" if fail_build else "0",
        "PYNCHY_TEST_PRUNE_COUNT": str(prune_count),
        "PYNCHY_TEST_RUNTIME_LOG": str(runtime_log),
        "PYNCHY_TEST_PYTHON": sys.executable,
    }
    result = subprocess.run(  # noqa: S603 - executable is the repository-owned build script.
        [str(project_root / "src" / "pynchy" / "agent" / "build.sh")],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return BuildScriptRun(
        result=result,
        runtime_calls=(
            runtime_log.read_text(encoding="utf-8").splitlines() if runtime_log.exists() else []
        ),
    )


def test_build_script_prunes_before_and_after_build(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path)

    assert result.result.returncode == 0
    assert result.runtime_calls[0] == "image prune -f"
    assert result.runtime_calls[-1] == "image prune -f"
    assert any(call.startswith("build -t pynchy-agent:latest") for call in result.runtime_calls)


def test_build_script_uses_narrow_mcp_contexts(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    mcp_dir = project_root / "src" / "pynchy" / "agent" / "mcp"
    integrations_dir = project_root / "src" / "pynchy" / "plugins" / "integrations"

    assert (
        f"build -t pynchy-mcp-gcal:latest -f {mcp_dir / 'gcal.Dockerfile'} {mcp_dir}"
        in result.runtime_calls
    )
    assert (
        f"build -t pynchy-mcp-gdrive:latest -f {mcp_dir / 'gdrive.Dockerfile'} {mcp_dir}"
        in result.runtime_calls
    )
    notebook_build = (
        f"build -t pynchy-mcp-notebook:latest -f {mcp_dir / 'notebook.Dockerfile'} "
        f"{integrations_dir}"
    )
    assert notebook_build in result.runtime_calls


def test_gdrive_dockerfile_matches_its_narrow_build_context() -> None:
    project_root = Path(__file__).resolve().parents[1]
    dockerfile = project_root / "src" / "pynchy" / "agent" / "mcp" / "gdrive.Dockerfile"

    assert "COPY gdrive-wrapper.mjs /app/gdrive-wrapper.mjs" in (
        dockerfile.read_text(encoding="utf-8")
    )


def test_build_script_refuses_build_when_preflight_prune_fails(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path, fail_prune_call=1)

    assert result.result.returncode == 1
    assert not any(call.startswith("build ") for call in result.runtime_calls)
    assert "Refusing to build with stale container build state." in result.result.stderr


def test_build_script_fails_when_postbuild_prune_fails(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path, fail_prune_call=2)

    assert result.result.returncode == 1
    assert any(call.startswith("build -t pynchy-agent:latest") for call in result.runtime_calls)
    assert "Container build-state cleanup failed." in result.result.stderr


def test_build_script_preserves_healthy_apple_builder(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path, runtime="apple")

    assert result.result.returncode == 0
    assert "builder stop" not in result.runtime_calls
    assert "builder rm --force" not in result.runtime_calls


def test_build_script_reclaims_apple_builder_after_a_failed_build(tmp_path: Path) -> None:
    result = _run_build_script(tmp_path, fail_build=True, runtime="apple")

    assert result.result.returncode == 1
    assert result.runtime_calls[-3:] == ["builder stop", "builder rm --force", "image prune"]
