"""Behavioral tests for the tracked Graphify graph wrapper."""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # noqa: S404 - tests invoke git in isolated temporary repositories.
import unicodedata
from typing import TYPE_CHECKING

import pytest
from scripts import graphify_graph

if TYPE_CHECKING:
    from pathlib import Path


def _normalize_id(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("_").casefold()


def _graph_bytes(scan_root: Path, *, relation: str = "indirect_call") -> bytes:
    source_file = "package/integration/_plugin.py"
    canonical = "package_integration_plugin"
    legacy = _normalize_id(str(scan_root / source_file))
    return json.dumps(
        {
            "nodes": [
                {
                    "id": canonical,
                    "label": "integration/_plugin.py",
                    "file_type": "code",
                    "source_file": source_file,
                    "source_location": "L1",
                },
                {
                    "id": "package_handlers_run",
                    "label": "run()",
                    "file_type": "code",
                    "source_file": "package/handlers.py",
                    "source_location": "L3",
                },
            ],
            "edges": [
                {
                    "source": legacy,
                    "target": "package_handlers_run",
                    "relation": relation,
                    "source_file": source_file,
                    "source_location": "L8",
                }
            ],
            "hyperedges": [],
            "input_tokens": 0,
            "output_tokens": 0,
        },
        indent=2,
    ).encode()


def _run_git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - fixed git command in an isolated test repo.
        [git, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_canonicalization_is_byte_stable_across_checkout_roots(tmp_path: Path) -> None:
    first_root = tmp_path / "first" / "src"
    second_root = tmp_path / "second" / "src"

    first = graphify_graph.canonicalize_graph(
        _graph_bytes(first_root),
        scan_root=first_root,
    )
    second = graphify_graph.canonicalize_graph(
        _graph_bytes(second_root),
        scan_root=second_root,
    )

    assert first.rewritten_endpoints == 1
    assert second.rewritten_endpoints == 1
    assert first.content == second.content
    assert json.loads(first.content)["edges"][0]["source"] == "package_integration_plugin"


def test_canonicalization_rejects_unrecognized_checkout_derived_endpoint(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "checkout" / "src"

    with pytest.raises(
        graphify_graph.GraphifyGraphError,
        match="checkout-derived edges\\[0\\]\\.source",
    ):
        graphify_graph.canonicalize_graph(
            _graph_bytes(scan_root, relation="calls"),
            scan_root=scan_root,
        )


def test_canonicalization_rejects_duplicate_node_ids(tmp_path: Path) -> None:
    scan_root = tmp_path / "checkout" / "src"
    data = json.loads(_graph_bytes(scan_root))
    data["nodes"].append(dict(data["nodes"][0]))

    with pytest.raises(graphify_graph.GraphifyGraphError, match="duplicate node id"):
        graphify_graph.canonicalize_graph(
            json.dumps(data).encode(),
            scan_root=scan_root,
        )


def test_canonicalization_allows_synthetic_nodes_without_source_files(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "checkout" / "src"
    data = json.loads(_graph_bytes(scan_root))
    data["nodes"].append(
        {
            "id": "external_symbol",
            "label": "external_symbol",
            "source_file": "",
        }
    )

    result = graphify_graph.canonicalize_graph(
        json.dumps(data).encode(),
        scan_root=scan_root,
    )

    assert not json.loads(result.content)["nodes"][-1]["source_file"]


def test_sync_staged_builds_only_from_git_index_and_stages_graph(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    _run_git(repo.parent, "init", repo.name)
    source.write_text("STAGED = True\n", encoding="utf-8")
    _run_git(repo, "add", "src/app.py")
    source.write_text("STAGED = False\n", encoding="utf-8")
    built_from: list[str] = []

    def build_from_snapshot(scan_root: Path) -> bytes:
        built_from.append((scan_root / "app.py").read_text(encoding="utf-8"))
        return (
            json.dumps(
                {
                    "nodes": [{"id": "app", "source_file": "app.py"}],
                    "edges": [],
                    "hyperedges": [],
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
                indent=2,
            )
            + "\n"
        ).encode()

    graphify_graph.sync_staged(repo, builder=build_from_snapshot)

    assert built_from == ["STAGED = True\n"]
    assert source.read_text(encoding="utf-8") == "STAGED = False\n"
    staged = set(_run_git(repo, "diff", "--cached", "--name-only").splitlines())
    assert staged == {"graphify-out/graph.json", "src/app.py"}
    assert _run_git(repo, "diff", "--", "src/app.py")


def test_update_rejects_nested_graphify_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "graphify-out" / "graph.json"
    nested.parent.mkdir(parents=True)
    _run_git(repo.parent, "init", repo.name)
    (repo / ".gitignore").write_text(
        "/graphify-out/*\n",
        encoding="utf-8",
    )
    nested.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        graphify_graph.GraphifyGraphError,
        match=re.escape("src/graphify-out/graph.json"),
    ):
        graphify_graph.update_graph(repo, builder=lambda _: b"{}\n")


def test_update_ignores_graphify_output_in_ignored_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ignored = repo / ".worktrees" / "feature" / "graphify-out" / "graph.json"
    ignored.parent.mkdir(parents=True)
    _run_git(repo.parent, "init", repo.name)
    (repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    ignored.write_text("{}\n", encoding="utf-8")

    graphify_graph.update_graph(repo, builder=lambda _: b"{}\n")

    assert (repo / "graphify-out" / "graph.json").read_bytes() == b"{}\n"
