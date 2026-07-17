"""Unit tests for the test-data-shape pre-commit check."""

from scripts.pre_commit_hooks.check_test_simple_namespace import find_simple_namespace_uses


def test_reports_direct_and_aliased_imports() -> None:
    source = "\n".join(
        (
            "from types import SimpleNamespace",
            "from types import SimpleNamespace as Data",
            "direct = SimpleNamespace()",
            "aliased = Data()",
        )
    )

    assert find_simple_namespace_uses(source) == [1, 2, 3, 4]


def test_reports_qualified_types_access() -> None:
    source = "\n".join(
        (
            "import types as stdlib_types",
            "item = stdlib_types.SimpleNamespace()",
        )
    )

    assert find_simple_namespace_uses(source) == [2]


def test_allows_declared_data_models() -> None:
    source = "\n".join(
        (
            "from dataclasses import dataclass",
            "@dataclass",
            "class Event:",
            "    name: str",
        )
    )

    assert find_simple_namespace_uses(source) == []
