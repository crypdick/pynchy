"""Tests for the private first-party test-boundary prek check."""

from scripts.prek_hooks.check_private_test_imports import (
    PrivateTestViolation,
    find_private_test_violations,
    unbaselined_violations,
)

_PYNCHY = {"pynchy"}


def _violations(source: str) -> list[PrivateTestViolation]:
    return find_private_test_violations(source, _PYNCHY)


def test_reports_private_symbol_and_module_imports() -> None:
    source = "\n".join(
        (
            "from pynchy.host._implementation import PublicThing, _helper",
            "import pynchy.plugins._private_module as private_module",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (1, "private-module-import", "pynchy.host._implementation"),
        (1, "private-symbol-import", "pynchy.host._implementation._helper"),
        (2, "private-module-import", "pynchy.plugins._private_module"),
    ]


def test_reports_private_attribute_but_not_patch_target() -> None:
    source = "\n".join(
        (
            "from pynchy import __main__ as cli",
            "from pynchy.host.gateway import Gateway",
            "gateway = Gateway()",
            "cli._doctor()",
            "gateway._credentials = {}",
            "patch_target = 'pynchy.host.gateway._request'",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (4, "private-attribute", "cli._doctor"),
        (5, "private-attribute", "gateway._credentials"),
    ]


def test_reports_private_attribute_on_a_directly_created_first_party_object() -> None:
    source = "\n".join(
        (
            "from pynchy.host.gateway import Gateway",
            "Gateway()._credentials = {}",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (2, "private-attribute", "Gateway()._credentials"),
    ]


def test_reports_dynamic_private_attributes_on_known_first_party_values() -> None:
    source = "\n".join(
        (
            "from pynchy.host.gateway import Gateway",
            "gateway = Gateway()",
            "getattr(gateway, '_credentials')",
            "hasattr(gateway, '_credentials')",
            "setattr(gateway, '_credentials', {})",
            "delattr(gateway, '_credentials')",
            "getattr(third_party, '_credentials')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (3, "private-dynamic-attribute", "gateway._credentials"),
        (4, "private-dynamic-attribute", "gateway._credentials"),
        (5, "private-dynamic-attribute", "gateway._credentials"),
        (6, "private-dynamic-attribute", "gateway._credentials"),
    ]


def test_reports_private_attribute_on_a_typed_first_party_fixture() -> None:
    source = "\n".join(
        (
            "from pynchy.host.orchestrator.app import PynchyApp",
            "async def test_group_processing(app: PynchyApp):",
            "    await app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (3, "private-attribute", "app._process_group_messages"),
    ]


def test_reports_private_attributes_through_wrapped_first_party_annotations() -> None:
    source = "\n".join(
        (
            "from typing import Annotated",
            "from pynchy.host.orchestrator.app import PynchyApp",
            "def test_group_processing(app: PynchyApp | None):",
            "    app._process_group_messages('chat@g.us')",
            "def build_app() -> Annotated[PynchyApp, 'fixture']:",
            "    return object()",
            "built = build_app()",
            "built._process_group_messages('chat@g.us')",
            "def test_forward_reference(app: 'PynchyApp | None'):",
            "    app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (4, "private-attribute", "app._process_group_messages"),
        (8, "private-attribute", "built._process_group_messages"),
        (10, "private-attribute", "app._process_group_messages"),
    ]


def test_reports_private_attributes_from_a_typed_local_factory() -> None:
    source = "\n".join(
        (
            "from pynchy.host.orchestrator.app import PynchyApp",
            "def build_app() -> PynchyApp:",
            "    return PynchyApp()",
            "app = build_app()",
            "app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (5, "private-attribute", "app._process_group_messages"),
    ]


def test_reports_private_attributes_from_a_test_class_factory() -> None:
    source = "\n".join(
        (
            "from pynchy.host.orchestrator.app import PynchyApp",
            "class TestApp:",
            "    def build_app(self):",
            "        return PynchyApp()",
            "    def test_group_processing(self):",
            "        app = self.build_app()",
            "        app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (7, "private-attribute", "app._process_group_messages"),
    ]


def test_reports_private_attributes_from_a_known_test_class_field() -> None:
    source = "\n".join(
        (
            "from pynchy.host.orchestrator.app import PynchyApp",
            "class TestApp:",
            "    def setup_method(self):",
            "        self.app = PynchyApp()",
            "    def test_group_processing(self):",
            "        self.app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (6, "private-attribute", "self.app._process_group_messages"),
    ]


def test_reports_private_attributes_through_a_known_first_party_alias() -> None:
    source = "\n".join(
        (
            "from pynchy.host.orchestrator.app import PynchyApp",
            "app = PynchyApp()",
            "alias = app",
            "alias._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (4, "private-attribute", "alias._process_group_messages"),
    ]


def test_reports_private_attributes_through_an_explicit_first_party_cast() -> None:
    source = "\n".join(
        (
            "from typing import cast",
            "from pynchy.host.orchestrator.app import PynchyApp",
            "app = cast(PynchyApp, object())",
            "app._process_group_messages('chat@g.us')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (4, "private-attribute", "app._process_group_messages"),
    ]


def test_reports_literal_dynamic_imports_of_private_first_party_modules() -> None:
    source = "\n".join(
        (
            "from importlib import import_module",
            "import importlib as imports",
            "first = import_module('pynchy.host._implementation')",
            "second = imports.import_module('pynchy.plugins._private_module')",
            "third = __import__('pynchy.agent._runtime')",
        )
    )

    assert [(item.line, item.kind, item.subject) for item in _violations(source)] == [
        (3, "private-dynamic-module-import", "pynchy.host._implementation"),
        (4, "private-dynamic-module-import", "pynchy.plugins._private_module"),
        (5, "private-dynamic-module-import", "pynchy.agent._runtime"),
    ]


def test_allows_dunders_and_non_first_party_private_names() -> None:
    source = "\n".join(
        (
            "from pynchy import __version__",
            "from third_party import _internal",
            "import third_party._private_module as private_module",
            "private_module._helper()",
        )
    )

    assert _violations(source) == []


def test_requires_a_reason_for_an_allow_marker() -> None:
    source = "\n".join(
        (
            "from pynchy import __main__ as cli",
            "cli._doctor()  # allow: private-test-imports",
        )
    )

    assert [(item.line, item.kind) for item in _violations(source)] == [
        (2, "private-attribute"),
        (2, "unjustified-allow"),
    ]


def test_allows_a_narrowly_justified_external_process_carve_out() -> None:
    source = "\n".join(
        (
            "from pynchy import __main__ as cli",
            "cli._doctor()  # allow: private-test-imports - external-process: process-only signal",
        )
    )

    assert _violations(source) == []


def test_rejects_an_allow_reason_that_is_not_an_external_process_boundary() -> None:
    source = "\n".join(
        (
            "from pynchy import __main__ as cli",
            "cli._doctor()  # allow: private-test-imports - decoder coverage",
        )
    )

    assert [(item.line, item.kind) for item in _violations(source)] == [
        (2, "private-attribute"),
        (2, "unjustified-allow"),
    ]


def test_does_not_treat_an_allow_marker_inside_a_string_as_a_comment() -> None:
    source = 'example = "# allow: private-test-imports - external-process: not a comment"'

    assert _violations(source) == []


def test_baseline_uses_ast_shape_and_occurrence_count() -> None:
    existing = _violations(
        "\n".join(
            (
                "from pynchy import __main__ as cli",
                "cli._doctor()",
            )
        )
    )
    changed = _violations(
        "\n".join(
            (
                "from pynchy import __main__ as cli",
                "cli._doctor()",
                "cli._doctor()",
                "cli._control_client_target()",
            )
        )
    )

    assert [(item.line, item.subject) for item in unbaselined_violations(changed, existing)] == [
        (3, "cli._doctor"),
        (4, "cli._control_client_target"),
    ]
