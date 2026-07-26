"""Contract tests for the generic architecture-boundary checker."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from scripts.prek_hooks.check_architecture_boundaries import check_architecture

if TYPE_CHECKING:
    from pathlib import Path


def _write(root: Path, relative: str, content: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")


def _policy(
    *,
    composition_roots: tuple[str, ...] = (),
    application_allowed: tuple[str, ...] = ("domain",),
    adapter_allowed: tuple[str, ...] = ("domain",),
) -> str:
    roots = ", ".join(f'"{item}"' for item in composition_roots)
    app_allowed = ", ".join(f'"{item}"' for item in application_allowed)
    adapter_allowed_text = ", ".join(f'"{item}"' for item in adapter_allowed)
    return f"""
        version = 1
        default_violation_guidance = "Use a configured generic boundary."
        source_roots = [
          {{ path = "src/acme", module = "acme", exclude = ["runner/**"] }},
          {{ path = "src/acme/runner/src/worker", module = "worker" }},
        ]
        composition_roots = [{roots}]

        [[components]]
        name = "domain"
        module_patterns = ["acme", "acme.domain.**"]
        allowed_dependencies = []
        violation_guidance = "Keep stable domain contracts inward."

        [[components]]
        name = "application"
        module_patterns = ["acme.app.**"]
        allowed_dependencies = [{app_allowed}]

        [[components]]
        name = "adapter"
        module_patterns = ["acme.adapters.**"]
        allowed_dependencies = [{adapter_allowed_text}]
        violation_guidance = "Depend on an application-owned port."

        [[components]]
        name = "worker"
        module_patterns = ["worker.**"]
        allowed_dependencies = []
        violation_guidance = "Cross this runtime through its configured wire contract."
    """


def _initialize_repo(
    root: Path,
    app_source: str,
    *,
    policy: str | None = None,
    baseline: str = "version = 1\n",
    app_module: str = "use_case",
) -> None:
    _write(root, "src/acme/__init__.py")
    _write(root, "src/acme/domain/__init__.py")
    _write(root, "src/acme/app/__init__.py")
    _write(root, f"src/acme/app/{app_module}.py", app_source)
    _write(root, "src/acme/adapters/__init__.py")
    _write(root, "src/acme/adapters/database.py")
    _write(root, "src/acme/adapters/cache.py")
    _write(root, "src/acme/adapters/dynamic.py")
    _write(root, "src/acme/runner/src/worker/__init__.py")
    _write(root, "src/acme/runner/src/worker/core.py")
    _write(root, "architecture.toml", policy or _policy())
    _write(root, "architecture-baseline.toml", baseline)


def _check(root: Path):
    return check_architecture(
        root,
        root / "architecture.toml",
        root / "architecture-baseline.toml",
    )


def test_extracts_runtime_type_checking_relative_and_dynamic_imports(tmp_path: Path) -> None:
    _initialize_repo(
        tmp_path,
        """
        from typing import TYPE_CHECKING
        from importlib import import_module as load

        from ..adapters import database

        if TYPE_CHECKING:
            from acme.adapters.cache import Cache

        dynamic = load("acme.adapters.dynamic")
        """,
    )

    diagnostics, current = _check(tmp_path)

    assert [
        (item.imported, item.kind) for item in current if item.importer == "acme.app.use_case"
    ] == [
        ("acme.adapters.database", "runtime-import"),
        ("acme.adapters.cache", "type-checking-import"),
        ("acme.adapters.dynamic", "runtime-import"),
    ]
    assert {item.code for item in diagnostics} == {"architecture-boundary"}
    assert all("Depend on an application-owned port." in item.message for item in diagnostics)
    assert all(item.path == "src/acme/app/use_case.py" for item in diagnostics)


def test_exact_baseline_allows_only_recorded_import_occurrences(tmp_path: Path) -> None:
    baseline = """
        version = 1

        [[violations]]
        importer = "acme.app.use_case"
        target_component = "adapter"
        kind = "runtime-import"
        count = 1
        imports = ["acme.adapters.database"]
        reason = "The use case still calls this database adapter directly."
    """
    _initialize_repo(tmp_path, "from acme.adapters import database", baseline=baseline)

    diagnostics, _current = _check(tmp_path)

    assert diagnostics == []

    _write(
        tmp_path,
        "src/acme/app/use_case.py",
        "from acme.adapters import cache",
    )
    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == [
        "architecture-baseline-stale",
        "architecture-boundary",
    ]
    assert "acme.adapters.cache" in diagnostics[1].message


def test_baseline_count_reason_and_import_list_must_agree(tmp_path: Path) -> None:
    baseline = """
        version = 1

        [[violations]]
        importer = "acme.app.use_case"
        target_component = "adapter"
        kind = "runtime-import"
        count = 2
        imports = ["acme.adapters.database"]
        reason = ""
    """
    _initialize_repo(tmp_path, "from acme.adapters import database", baseline=baseline)

    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == ["architecture-baseline"]
    assert "positive count matching imports and a nonempty reason" in diagnostics[0].message


def test_named_composition_root_may_wire_a_concrete_adapter(tmp_path: Path) -> None:
    _initialize_repo(
        tmp_path,
        "from acme.adapters import database",
        policy=_policy(composition_roots=("acme.app.bootstrap",)),
        app_module="bootstrap",
    )

    diagnostics, current = _check(tmp_path)

    assert diagnostics == []
    assert current == []


def test_policy_rejects_unclassified_and_multiply_classified_modules(tmp_path: Path) -> None:
    overlapping_policy = (
        _policy()
        + """
        [[components]]
        name = "overlap"
        module_patterns = ["acme.app.**"]
        allowed_dependencies = []
    """
    )
    _initialize_repo(tmp_path, "", policy=overlapping_policy)
    _write(tmp_path, "src/acme/unowned.py")

    diagnostics, _current = _check(tmp_path)

    policy_messages = [item.message for item in diagnostics if item.code == "architecture-policy"]
    assert any("'acme.unowned' is unclassified" in message for message in policy_messages)
    assert any(
        "'acme.app.use_case' is matched application, overlap" in message
        for message in policy_messages
    )


def test_policy_rejects_cycles_in_the_allowed_component_graph(tmp_path: Path) -> None:
    _initialize_repo(
        tmp_path,
        "",
        policy=_policy(
            application_allowed=("domain", "adapter"),
            adapter_allowed=("domain", "application"),
        ),
    )

    diagnostics, _current = _check(tmp_path)

    cycles = [item for item in diagnostics if item.code == "architecture-cycle"]
    assert len(cycles) == 1
    assert "adapter -> application -> adapter" in cycles[0].message


def test_separate_source_root_uses_policy_guidance_without_project_assumptions(
    tmp_path: Path,
) -> None:
    _initialize_repo(tmp_path, "from worker import core")

    diagnostics, current = _check(tmp_path)

    assert [(item.importer, item.imported) for item in current] == [
        ("acme.app.use_case", "worker.core")
    ]
    assert len(diagnostics) == 1
    assert "Cross this runtime through its configured wire contract." in diagnostics[0].message
