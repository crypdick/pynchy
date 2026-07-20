"""Package manifest and lockfile artifact security tests."""

from __future__ import annotations

import pytest

from agent_runner.security.artifacts import deterministic_findings, normalize_tool_request
from agent_runner.security.packages import PackageSource


def test_patch_header_routes_manifest_content_through_package_parser() -> None:
    request = normalize_tool_request(
        "apply_patch",
        {
            "patch": """*** Begin Patch
*** Update File: pyproject.toml
@@
+dependencies = [\"httpx==0.28.1\"]
*** End Patch"""
        },
    )

    assert request.packages[0].name == "httpx"
    assert request.packages[0].version == "0.28.1"


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (
            "requirements.txt",
            "--index-url https://packages.example/simple\nhttpx==0.28.1",
        ),
        (
            "requirements.txt",
            "--no-index\n--find-links /tmp/wheels\nhttpx==0.28.1",
        ),
        (
            "pyproject.toml",
            """[project]
dependencies = ["httpx==0.28.1"]

[[tool.uv.index]]
name = "internal"
url = "https://packages.example/simple"
""",
        ),
        (
            "pyproject.toml",
            """[project]
dependencies = ["httpx==0.28.1"]

[tool.uv.sources]
httpx = { index = "internal" }
""",
        ),
    ],
)
def test_manifest_custom_registry_configuration_requires_approval(
    path: str,
    content: str,
) -> None:
    request = normalize_tool_request("Write", {"file_path": path, "content": content})

    assert request.packages[0].source is PackageSource.CUSTOM_REGISTRY
    assert "PKG001" in {finding.rule_id for finding in deterministic_findings(request)}


@pytest.mark.parametrize(
    ("path", "official_content", "custom_content"),
    [
        (
            "uv.lock",
            (
                '[[package]]\nname = "httpx"\nversion = "0.28.1"\n'
                'source = { registry = "https://pypi.org/simple" }'
            ),
            (
                '[[package]]\nname = "httpx"\nversion = "0.28.1"\n'
                'source = { registry = "https://packages.example/simple" }'
            ),
        ),
        (
            "package-lock.json",
            '{"packages":{"node_modules/react":{"version":"19.1.0","resolved":"https://registry.npmjs.org/react/-/react-19.1.0.tgz"}}}',
            '{"packages":{"node_modules/react":{"version":"19.1.0","resolved":"https://packages.example/react.tgz"}}}',
        ),
        (
            "yarn.lock",
            'react@^19.1.0:\n  version "19.1.0"\n  resolved "https://registry.yarnpkg.com/react/-/react-19.1.0.tgz"',
            'react@^19.1.0:\n  version "19.1.0"\n  resolved "https://packages.example/react.tgz"',
        ),
        (
            "Cargo.lock",
            '[[package]]\nname = "serde"\nversion = "1.0.219"\nsource = "registry+https://github.com/rust-lang/crates.io-index"',
            '[[package]]\nname = "serde"\nversion = "1.0.219"\nsource = "registry+https://packages.example/cargo-index"',
        ),
    ],
)
def test_lockfile_source_distinguishes_authoritative_and_custom_registries(
    path: str,
    official_content: str,
    custom_content: str,
) -> None:
    official = normalize_tool_request(
        "Write",
        {"file_path": path, "content": official_content},
    )
    custom = normalize_tool_request(
        "Write",
        {"file_path": path, "content": custom_content},
    )

    assert official.packages[0].source is PackageSource.REGISTRY
    assert custom.packages[0].source is PackageSource.CUSTOM_REGISTRY
    assert "PKG001" in {finding.rule_id for finding in deterministic_findings(custom)}
