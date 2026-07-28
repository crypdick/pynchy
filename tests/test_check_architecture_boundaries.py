"""Contract tests for the generic architecture-boundary checker."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
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
        version = 2
        default_violation_guidance = "Use a configured generic boundary."
        root_module_max_inbound_importers = 20
        source_roots = [
          {{ path = "src/acme", module = "acme", exclude = ["runner/**"] }},
          {{ path = "src/acme/runner/src/worker", module = "worker" }},
        ]
        composition_roots = [{roots}]

        [roles.domain]
        allowed_dependencies = []
        violation_guidance = "Keep stable domain contracts inward."

        [roles.application]
        allowed_dependencies = [{app_allowed}]

        [roles.adapter]
        allowed_dependencies = [{adapter_allowed_text}]
        violation_guidance = "Depend on an application-owned port."

        [roles.worker]
        allowed_dependencies = []
        violation_guidance = "Cross this runtime through its configured wire contract."

        [[packages]]
        name = "root"
        root = "acme"
        role = "domain"
        public_modules = []
        include_descendants = false

        [[packages]]
        name = "domain"
        root = "acme.domain"
        role = "domain"
        public_modules = ["acme.domain.api"]

        [[packages]]
        name = "application"
        root = "acme.app"
        role = "application"
        public_modules = ["acme.app.api"]

        [[packages]]
        name = "adapters"
        root = "acme.adapters"
        role = "adapter"
        public_modules = ["acme.adapters.api"]

        [[packages]]
        name = "worker"
        root = "worker"
        role = "worker"
        public_modules = []
    """


def _initialize_repo(
    root: Path,
    app_source: str,
    *,
    policy: str | None = None,
    baseline: str = "version = 2\n",
    app_module: str = "use_case",
) -> None:
    _write(root, "src/acme/__init__.py")
    _write(root, "src/acme/domain/__init__.py")
    _write(root, "src/acme/domain/api.py")
    _write(root, "src/acme/app/__init__.py")
    _write(root, "src/acme/app/api.py")
    _write(root, f"src/acme/app/{app_module}.py", app_source)
    _write(root, "src/acme/adapters/__init__.py")
    _write(root, "src/acme/adapters/api.py")
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


def _with_root_module(
    policy: str,
    module: str,
    *,
    allow_unbounded_inbound_imports: bool = False,
) -> str:
    override = (
        f"""\n[file_overrides."src/acme/{module}.py"]\nallow_unbounded_inbound_imports = true\n"""
        if allow_unbounded_inbound_imports
        else ""
    )
    return (
        policy
        + override
        + f'''
        [[packages]]
        name = "{module}"
        root = "acme.{module}"
        role = "domain"
        public_modules = ["acme.{module}"]
        '''
    )


def _add_root_module_importers(root: Path, module: str, count: int) -> None:
    _write(root, f"src/acme/{module}.py")
    for index in range(count):
        _write(
            root,
            f"src/acme/app/consumer_{index}.py",
            f"from acme import {module}",
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
        (item.imported, item.kind, item.rule)
        for item in current
        if item.importer == "acme.app.use_case"
    ] == [
        ("acme.adapters.database", "runtime-import", "direction"),
        ("acme.adapters.database", "runtime-import", "visibility"),
        ("acme.adapters.cache", "type-checking-import", "direction"),
        ("acme.adapters.cache", "type-checking-import", "visibility"),
        ("acme.adapters.dynamic", "runtime-import", "direction"),
        ("acme.adapters.dynamic", "runtime-import", "visibility"),
    ]
    assert {item.code for item in diagnostics} == {
        "architecture-direction",
        "architecture-visibility",
    }
    assert all(item.path == "src/acme/app/use_case.py" for item in diagnostics)


def test_public_surface_and_role_direction_are_independent(tmp_path: Path) -> None:
    _initialize_repo(
        tmp_path,
        "from acme.adapters import api",
        policy=_policy(application_allowed=("domain", "adapter")),
    )

    diagnostics, current = _check(tmp_path)

    assert diagnostics == []
    assert current == []

    _write(tmp_path, "src/acme/app/use_case.py", "from acme.adapters import database")
    diagnostics, current = _check(tmp_path)

    assert [(item.imported, item.rule) for item in current] == [
        ("acme.adapters.database", "visibility")
    ]
    assert [item.code for item in diagnostics] == ["architecture-visibility"]
    assert "declared public modules: acme.adapters.api" in diagnostics[0].message
    assert "expose names through acme.adapters.api" in diagnostics[0].message


def test_exact_baseline_allows_only_recorded_rule_and_import_occurrences(
    tmp_path: Path,
) -> None:
    baseline = """
        version = 2

        [[violations]]
        importer = "acme.app.use_case"
        target_role = "adapter"
        rule = "direction"
        kind = "runtime-import"
        count = 1
        imports = ["acme.adapters.api"]
        reason = "The use case still calls this adapter facade directly."
    """
    _initialize_repo(tmp_path, "from acme.adapters import api", baseline=baseline)

    diagnostics, _current = _check(tmp_path)

    assert diagnostics == []

    _write(tmp_path, "src/acme/app/use_case.py", "from acme.adapters import cache")
    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == [
        "architecture-baseline-stale",
        "architecture-direction",
        "architecture-visibility",
    ]
    assert "acme.adapters.cache" in diagnostics[1].message


def test_baseline_rule_count_reason_and_import_list_must_agree(tmp_path: Path) -> None:
    baseline = """
        version = 2

        [[violations]]
        importer = "acme.app.use_case"
        target_role = "adapter"
        rule = "everything"
        kind = "runtime-import"
        count = 2
        imports = ["acme.adapters.api"]
        reason = ""
    """
    _initialize_repo(tmp_path, "from acme.adapters import api", baseline=baseline)

    diagnostics, _current = _check(tmp_path)

    baseline_diagnostics = [item for item in diagnostics if item.code == "architecture-baseline"]
    assert len(baseline_diagnostics) == 1
    assert "direction or visibility rule" in baseline_diagnostics[0].message


def test_named_composition_root_may_wire_a_private_concrete_adapter(
    tmp_path: Path,
) -> None:
    _initialize_repo(
        tmp_path,
        "from acme.adapters import database",
        policy=_policy(composition_roots=("acme.app.bootstrap",)),
        app_module="bootstrap",
    )

    diagnostics, current = _check(tmp_path)

    assert diagnostics == []
    assert current == []


def test_policy_rejects_unclassified_modules_and_duplicate_package_roots(
    tmp_path: Path,
) -> None:
    duplicate_policy = (
        _policy()
        + """
        [[packages]]
        name = "duplicate"
        root = "acme.app"
        role = "application"
        public_modules = []
    """
    )
    _initialize_repo(tmp_path, "", policy=duplicate_policy)
    _write(tmp_path, "src/acme/unowned.py")

    diagnostics, _current = _check(tmp_path)

    policy_messages = [item.message for item in diagnostics if item.code == "architecture-policy"]
    assert any("'acme.unowned' is unclassified" in message for message in policy_messages)
    assert any("duplicate package root 'acme.app'" in message for message in policy_messages)


def test_policy_rejects_cycles_in_the_allowed_role_graph(tmp_path: Path) -> None:
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


def test_package_family_expands_only_direct_child_packages(tmp_path: Path) -> None:
    family_policy = (
        _policy(application_allowed=("domain", "adapter"))
        + """
        [[packages]]
        name = "plugins"
        root = "acme.plugins"
        role = "domain"
        public_modules = []
        include_descendants = false

        [[package_families]]
        name = "plugins"
        root_pattern = "acme.plugins.*"
        role = "adapter"
        public_modules = ["{root}.api"]
    """
    )
    _initialize_repo(
        tmp_path,
        """
        from acme.plugins.alpha import api, internal
        from acme.plugins.alpha import helpers
        from acme.plugins.beta import api as beta_api
        """,
        policy=family_policy,
    )
    _write(tmp_path, "src/acme/plugins/__init__.py")
    _write(tmp_path, "src/acme/plugins/alpha/__init__.py")
    _write(tmp_path, "src/acme/plugins/alpha/api.py")
    _write(tmp_path, "src/acme/plugins/alpha/internal.py")
    _write(tmp_path, "src/acme/plugins/alpha/helpers/__init__.py")
    _write(tmp_path, "src/acme/plugins/beta/__init__.py")
    _write(tmp_path, "src/acme/plugins/beta/api.py")

    diagnostics, current = _check(tmp_path)

    assert [(item.imported, item.target_package, item.rule) for item in current] == [
        ("acme.plugins.alpha.internal", "acme.plugins.alpha", "visibility"),
        ("acme.plugins.alpha.helpers", "acme.plugins.alpha", "visibility"),
    ]
    assert [item.code for item in diagnostics] == [
        "architecture-visibility",
        "architecture-visibility",
    ]


def test_public_packages_with_the_same_role_need_explicit_direction(
    tmp_path: Path,
) -> None:
    family_policy = (
        _policy(application_allowed=("domain", "adapter"))
        + """
        [[packages]]
        name = "plugins"
        root = "acme.plugins"
        role = "domain"
        public_modules = []
        include_descendants = false

        [[package_families]]
        name = "plugins"
        root_pattern = "acme.plugins.*"
        role = "adapter"
        public_modules = ["{root}.api"]
    """
    )
    _initialize_repo(tmp_path, "", policy=family_policy)
    _write(tmp_path, "src/acme/plugins/__init__.py")
    _write(tmp_path, "src/acme/plugins/alpha/__init__.py")
    _write(
        tmp_path,
        "src/acme/plugins/alpha/api.py",
        "from acme.plugins.beta import api",
    )
    _write(tmp_path, "src/acme/plugins/beta/__init__.py")
    _write(tmp_path, "src/acme/plugins/beta/api.py")

    diagnostics, current = _check(tmp_path)

    assert [(item.imported, item.rule) for item in current] == [
        ("acme.plugins.beta.api", "direction")
    ]
    assert [item.code for item in diagnostics] == ["architecture-direction"]


def test_package_family_rejects_recursive_pattern(tmp_path: Path) -> None:
    policy = (
        _policy()
        + """
        [[package_families]]
        name = "plugins"
        root_pattern = "acme.plugins.**"
        role = "adapter"
        public_modules = ["{root}.api"]
    """
    )
    _initialize_repo(tmp_path, "", policy=policy)

    with pytest.raises(ValueError, match="one-level"):
        _check(tmp_path)


def test_separate_source_root_uses_role_guidance_without_project_assumptions(
    tmp_path: Path,
) -> None:
    _initialize_repo(tmp_path, "from worker import core")

    diagnostics, current = _check(tmp_path)

    assert [(item.importer, item.imported, item.rule) for item in current] == [
        ("acme.app.use_case", "worker.core", "direction"),
        ("acme.app.use_case", "worker.core", "visibility"),
    ]
    direction = [item for item in diagnostics if item.code == "architecture-direction"]
    assert len(direction) == 1
    assert "Cross this runtime through its configured wire contract." in direction[0].message


def test_root_module_inbound_importer_budget_rejects_a_new_hub(tmp_path: Path) -> None:
    """A root module cannot acquire more direct importers than the configured budget."""
    _initialize_repo(
        tmp_path,
        "",
        policy=_with_root_module(_policy(), "shared"),
    )
    _add_root_module_importers(tmp_path, "shared", 21)

    diagnostics, _current = _check(tmp_path)

    assert [(item.path, item.code) for item in diagnostics] == [
        ("src/acme/shared.py", "architecture-root-module-inbound-importers")
    ]
    assert "21 direct inbound importers; limit is 20" in diagnostics[0].message


def test_root_module_exact_file_override_allows_unbounded_importers(tmp_path: Path) -> None:
    """An explicit root-file override exempts only that module from the importer budget."""
    _initialize_repo(
        tmp_path,
        "",
        policy=_with_root_module(
            _policy(),
            "shared",
            allow_unbounded_inbound_imports=True,
        ),
    )
    _add_root_module_importers(tmp_path, "shared", 21)

    diagnostics, _current = _check(tmp_path)

    assert diagnostics == []


def test_root_module_importer_baseline_rejects_growth_and_requires_shrinking(
    tmp_path: Path,
) -> None:
    """A root-module importer baseline rejects growth and becomes stale after reduction."""
    baseline = """
        version = 2

        [[root_module_inbound_imports]]
        path = "src/acme/shared.py"
        count = 20
        reason = "The shared module has not been split yet."
    """
    _initialize_repo(
        tmp_path,
        "",
        policy=_with_root_module(_policy(), "shared"),
        baseline=baseline,
    )
    _add_root_module_importers(tmp_path, "shared", 21)

    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == ["architecture-root-module-inbound-importers"]

    _write(
        tmp_path,
        "architecture-baseline.toml",
        baseline.replace("count = 20", "count = 22"),
    )
    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == ["architecture-baseline-stale"]
    assert "records 22 importers but source now has 21" in diagnostics[0].message


def test_logger_override_does_not_relax_outbound_dependency_direction(tmp_path: Path) -> None:
    """A logger-style inbound override leaves the root module's outgoing role policy intact."""
    _initialize_repo(
        tmp_path,
        "",
        policy=_with_root_module(
            _policy(),
            "logger",
            allow_unbounded_inbound_imports=True,
        ),
    )
    _write(tmp_path, "src/acme/logger.py", "from acme.app import api")
    for index in range(21):
        _write(
            tmp_path,
            f"src/acme/app/consumer_{index}.py",
            "from acme import logger",
        )

    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == ["architecture-direction"]
    assert diagnostics[0].path == "src/acme/logger.py"


def test_file_override_requires_an_exact_root_module_path(tmp_path: Path) -> None:
    """A file override cannot silently target a nested implementation module."""
    _initialize_repo(
        tmp_path,
        "",
        policy=(
            _policy()
            + """
            [file_overrides."src/acme/app/use_case.py"]
            allow_unbounded_inbound_imports = true
            """
        ),
    )

    diagnostics, _current = _check(tmp_path)

    assert [item.code for item in diagnostics] == ["architecture-policy"]
    assert "must name an importable root-level module" in diagnostics[0].message
