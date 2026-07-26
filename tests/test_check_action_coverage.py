"""Behavior tests for the fast semantic action marker audit."""

import os
import tomllib
from pathlib import Path

import pytest
from scripts.prek_hooks.check_action_coverage import (
    ActionCoverageAuditError,
    audit_action_coverage,
    collect_marked_tests,
    extract_action_markers,
    pytest_collection_command,
)

from pynchy.actions import ActionId, ActionSpec


def _spec(action_id: str) -> ActionSpec:
    return ActionSpec(ActionId(action_id), "tests", f"Exercise {action_id}.")


def test_extracts_decorator_and_parameter_markers_with_pytest_alias() -> None:
    source = """
import pytest as pt

@pt.mark.action("task.create", "task.cancel")
class TestTasks:
    pass

CASES = [
    pt.param(1, marks=pt.mark.action("task.list")),
    pt.param(
        2,
        marks=(pt.mark.slow, pt.mark.action("task.pause", "task.resume")),
    ),
]
"""

    assert extract_action_markers(source, path=Path("test_tasks.py")) == (
        "task.create",
        "task.cancel",
        "task.list",
        "task.pause",
        "task.resume",
    )


def test_extracts_imported_pytest_mark_alias() -> None:
    source = """
from pytest import mark as pm

@pm.action("task.create")
def test_create():
    pass
"""

    assert extract_action_markers(source, path=Path("test_tasks.py")) == ("task.create",)


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        (
            "@pytest.mark.action(ACTION_ID)\ndef test_dynamic():\n    pass\n",
            "action marker IDs must be string literals",
        ),
        (
            (
                "from pytest import mark as pm\n"
                "@pm.action(ACTION_ID)\n"
                "def test_dynamic_alias():\n"
                "    pass\n"
            ),
            "action marker IDs must be string literals",
        ),
        (
            '@pytest.mark.action(action_id="task.create")\ndef test_keyword():\n    pass\n',
            "action marker keyword arguments are not supported",
        ),
        (
            "@pytest.mark.action()\ndef test_empty():\n    pass\n",
            "action marker requires at least one string literal ID",
        ),
        (
            'pytest.mark.action("task.create")\n',
            "action marker must decorate a function/class",
        ),
    ],
)
def test_rejects_unauditable_action_markers(marker: str, message: str) -> None:
    source = f"import pytest\nACTION_ID = 'task.create'\n{marker}"

    with pytest.raises(ActionCoverageAuditError, match=message):
        extract_action_markers(source, path=Path("test_invalid.py"))


def test_rejects_direct_action_marker_factory_alias() -> None:
    source = """
import pytest

action = pytest.mark.action

@action("task.create")
def test_create():
    pass
"""

    with pytest.raises(ActionCoverageAuditError, match="factory aliases are not supported"):
        extract_action_markers(source, path=Path("test_invalid.py"))


def test_audit_reports_missing_and_unknown_literal_actions(tmp_path: Path) -> None:
    test_file = tmp_path / "test_actions.py"
    test_file.write_text(
        'from pytest import mark as pm\n@pm.action("task.unknown")\ndef test_action():\n    pass\n',
        encoding="utf-8",
    )

    with pytest.raises(ActionCoverageAuditError) as error:
        audit_action_coverage([tmp_path], (_spec("task.required"),))

    assert str(error.value) == (
        "Action coverage incomplete: actions without hermetic tests: task.required; "
        "tests mark unknown actions: task.unknown"
    )


def test_audit_reports_test_file_syntax_errors(tmp_path: Path) -> None:
    test_file = tmp_path / "test_broken.py"
    test_file.write_text("def test_broken(:\n", encoding="utf-8")

    with pytest.raises(ActionCoverageAuditError, match="invalid Python syntax"):
        audit_action_coverage([tmp_path], ())


def test_collection_command_scopes_pytest_to_marker_files() -> None:
    command = pytest_collection_command((Path("tests/test_one.py"), Path("tests/test_two.py")))

    assert command[1:] == (
        "-m",
        "pytest",
        "--collect-only",
        "-n",
        "0",
        "--action-coverage",
        "-qq",
        "tests/test_one.py",
        "tests/test_two.py",
    )


def test_collection_command_rejects_an_empty_marker_file_set() -> None:
    with pytest.raises(ActionCoverageAuditError, match="refusing to collect"):
        pytest_collection_command(())


def test_collection_failure_preserves_environment_and_clears_pytest_addopts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedCollection:
        returncode = 7

    received: dict[str, object] = {}

    def fake_run(command, *, check, env):
        received.update(command=command, check=check, env=env)
        return FailedCollection()

    monkeypatch.setenv("PYTEST_ADDOPTS", "tests")
    monkeypatch.setenv("ACTION_COVERAGE_TEST_SENTINEL", "preserved")
    monkeypatch.setattr(
        "scripts.prek_hooks.check_action_coverage.subprocess.run",
        fake_run,
    )

    assert collect_marked_tests((Path("tests/test_one.py"),)) == 7
    assert received["check"] is False
    environment = received["env"]
    assert isinstance(environment, dict)
    assert not environment["PYTEST_ADDOPTS"]
    assert environment["ACTION_COVERAGE_TEST_SENTINEL"] == "preserved"
    assert environment is not os.environ


def test_prek_action_gate_runs_for_deletion_only_commits() -> None:
    config = tomllib.loads(Path("prek.toml").read_text(encoding="utf-8"))
    hooks = next(repo["hooks"] for repo in config["repos"] if repo["repo"] == "local")
    action_coverage = next(hook for hook in hooks if hook["id"] == "action-coverage")

    assert action_coverage["always_run"] is True
