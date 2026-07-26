"""Build and verify Pynchy's portable, tracked Graphify graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404 - fixed local uvx and git commands implement the build.
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GRAPHIFY_VERSION = "0.9.26"
GRAPHIFY_PYTHON = "3.13"
GRAPH_PATH = Path("graphify-out/graph.json")
SOURCE_PATH = Path("src")

GraphBuilder = Callable[[Path], bytes]


class GraphifyGraphError(RuntimeError):
    """Raised when the graph cannot be built or proven portable."""


@dataclass(frozen=True)
class CanonicalGraph:
    """Validated graph bytes and the number of repaired upstream endpoints."""

    content: bytes
    rewritten_endpoints: int


def _normalize_id(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("_").casefold()


def _source_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.name:
        return None
    return path


def _canonical_file_id(source_file: PurePosixPath) -> str:
    return _normalize_id(source_file.with_suffix("").as_posix())


def _legacy_file_id(scan_root: Path, source_file: PurePosixPath) -> str:
    return _normalize_id(str(scan_root.joinpath(*source_file.parts)))


def _object_list(data: dict[str, object], key: str) -> list[dict[str, object]]:
    value = data.get(key)
    if not isinstance(value, list):
        raise GraphifyGraphError(f"Graphify graph must contain a {key!r} list")
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise GraphifyGraphError(f"Graphify {key}[{index}] must be an object")
        result.append(item)
    return result


def _node_ids(nodes: list[dict[str, object]]) -> set[str]:
    result: set[str] = set()
    for index, node in enumerate(nodes):
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphifyGraphError(f"Graphify nodes[{index}] has no non-empty string id")
        if node_id in result:
            raise GraphifyGraphError(f"Graphify emitted duplicate node id: {node_id}")
        result.add(node_id)
    if not result:
        raise GraphifyGraphError("Graphify emitted an empty graph")
    return result


def _repair_indirect_call_sources(
    edges: list[dict[str, object]],
    *,
    node_ids: set[str],
    scan_root: Path,
) -> int:
    rewritten = 0
    for edge in edges:
        if edge.get("relation") != "indirect_call":
            continue
        source = edge.get("source")
        if not isinstance(source, str) or source in node_ids:
            continue
        source_file = _source_path(edge.get("source_file"))
        if source_file is None or source != _legacy_file_id(scan_root, source_file):
            continue
        canonical = _canonical_file_id(source_file)
        if canonical not in node_ids:
            continue
        edge["source"] = canonical
        rewritten += 1
    return rewritten


def _validate_portability(
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    *,
    scan_root: Path,
) -> None:
    checkout_prefix = _normalize_id(str(scan_root))
    for collection_name, records in (("nodes", nodes), ("edges", edges)):
        for index, record in enumerate(records):
            fields = ("id",) if collection_name == "nodes" else ("source", "target")
            for field in fields:
                value = record.get(field)
                if not isinstance(value, str) or not value:
                    raise GraphifyGraphError(
                        f"Graphify {collection_name}[{index}].{field} must be a non-empty string"
                    )
                if value == checkout_prefix or value.startswith(f"{checkout_prefix}_"):
                    raise GraphifyGraphError(
                        f"Graphify left a checkout-derived {collection_name}[{index}].{field}: "
                        f"{value}"
                    )
            source_file = record.get("source_file")
            if source_file not in (None, "") and _source_path(source_file) is None:
                raise GraphifyGraphError(
                    f"Graphify {collection_name}[{index}].source_file is not portable: "
                    f"{source_file!r}"
                )


def canonicalize_graph(raw: bytes, *, scan_root: Path) -> CanonicalGraph:
    """Repair the verified upstream path bug and validate the persisted graph."""
    try:
        loaded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphifyGraphError(f"Graphify emitted invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise GraphifyGraphError("Graphify graph must be a JSON object")

    data: dict[str, object] = loaded
    nodes = _object_list(data, "nodes")
    edges = _object_list(data, "edges")
    _object_list(data, "hyperedges")
    node_ids = _node_ids(nodes)
    rewritten = _repair_indirect_call_sources(edges, node_ids=node_ids, scan_root=scan_root)
    _validate_portability(nodes, edges, scan_root=scan_root)

    content = f"{json.dumps(data, indent=2)}\n".encode()
    return CanonicalGraph(content=content, rewritten_endpoints=rewritten)


def _executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise GraphifyGraphError(f"Required executable is not available: {name}")
    return executable


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - command executables resolve through _executable.
        command,
        cwd=cwd,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if capture_output else ""
        suffix = f": {detail}" if detail else ""
        raise GraphifyGraphError(f"Command failed with exit code {result.returncode}{suffix}")
    return result


def build_graph(scan_root: Path) -> bytes:
    """Run pinned Graphify in isolation and return canonical graph bytes."""
    with tempfile.TemporaryDirectory(prefix="pynchy-graphify-build-") as temp:
        output_root = Path(temp)
        command = [
            _executable("uvx"),
            "--python",
            GRAPHIFY_PYTHON,
            "--from",
            f"graphifyy=={GRAPHIFY_VERSION}",
            "graphify",
            "extract",
            str(scan_root),
            "--code-only",
            "--no-cluster",
            "--force",
            "--out",
            str(output_root),
            "--timing",
        ]
        _run(command, cwd=output_root)
        generated = output_root / GRAPH_PATH
        if not generated.is_file():
            raise GraphifyGraphError(f"Graphify did not create {GRAPH_PATH}")
        graph = canonicalize_graph(generated.read_bytes(), scan_root=scan_root)
        if graph.rewritten_endpoints:
            print(
                f"Canonicalized {graph.rewritten_endpoints} path-derived "
                "Graphify indirect_call source(s)."
            )
        return graph.content


def _atomic_write(path: Path, content: bytes) -> bool:
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        Path(temporary).replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def _build_index_graph(repo_root: Path, *, builder: GraphBuilder) -> bytes:
    with tempfile.TemporaryDirectory(prefix="pynchy-graphify-index-") as temp:
        snapshot_root = Path(temp)
        prefix = f"{snapshot_root}{os.sep}"
        _run(
            [_executable("git"), "checkout-index", "--all", f"--prefix={prefix}"],
            cwd=repo_root,
        )
        scan_root = snapshot_root / SOURCE_PATH
        if not scan_root.is_dir():
            raise GraphifyGraphError("The Git index does not contain the Pynchy src directory")
        return builder(scan_root)


def _reject_nested_graphify_output(repo_root: Path) -> None:
    result = _run(
        [
            _executable("git"),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo_root,
        capture_output=True,
    )
    nested = sorted(
        path
        for path in result.stdout.split("\0")
        if path
        and "graphify-out" in PurePosixPath(path).parts
        and PurePosixPath(path).parts[0] != "graphify-out"
    )
    if nested:
        preview = ", ".join(nested[:5])
        suffix = ", ..." if len(nested) > 5 else ""
        raise GraphifyGraphError(
            "Graphify output is supported only at the repository root; "
            f"remove nested output before rebuilding: {preview}{suffix}"
        )


def update_graph(repo_root: Path, *, builder: GraphBuilder = build_graph) -> None:
    """Rebuild the graph from the working tree without staging it."""
    _reject_nested_graphify_output(repo_root)
    changed = _atomic_write(repo_root / GRAPH_PATH, builder(repo_root / SOURCE_PATH))
    print("Updated graphify-out/graph.json." if changed else "Graphify graph is already current.")


def check_graph(repo_root: Path, *, builder: GraphBuilder = build_graph) -> None:
    """Fail when the tracked graph differs from a clean working-tree rebuild."""
    _reject_nested_graphify_output(repo_root)
    target = repo_root / GRAPH_PATH
    if not target.is_file():
        raise GraphifyGraphError(f"Tracked graph is missing: {GRAPH_PATH}")
    expected = builder(repo_root / SOURCE_PATH)
    actual = target.read_bytes()
    if actual == expected:
        print("Graphify graph is current.")
        return
    raise GraphifyGraphError(
        "Tracked Graphify graph is stale "
        f"(tracked={hashlib.sha256(actual).hexdigest()[:12]}, "
        f"rebuilt={hashlib.sha256(expected).hexdigest()[:12]}). "
        "Run `uv run python scripts/graphify_graph.py update`."
    )


def sync_staged(repo_root: Path, *, builder: GraphBuilder = build_graph) -> None:
    """Rebuild from the Git index and stage only the owned graph artifact."""
    _reject_nested_graphify_output(repo_root)
    content = _build_index_graph(repo_root, builder=builder)
    changed = _atomic_write(repo_root / GRAPH_PATH, content)
    _run(
        [_executable("git"), "add", "--", GRAPH_PATH.as_posix()],
        cwd=repo_root,
    )
    message = "Updated and staged" if changed else "Verified and staged"
    print(f"{message} {GRAPH_PATH.as_posix()} from the Git index snapshot.")


def _repo_root() -> Path:
    result = _run(
        [_executable("git"), "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
    )
    return Path(result.stdout.strip()).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("update", "check", "sync-staged"),
        help="update the working graph, check freshness, or rebuild and stage from the Git index",
    )
    return parser


def _dispatch(command: str, repo_root: Path) -> None:
    if command == "update":
        update_graph(repo_root)
    elif command == "check":
        check_graph(repo_root)
    else:
        sync_staged(repo_root)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _dispatch(args.command, _repo_root())
    except GraphifyGraphError as exc:
        print(f"graphify-graph: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
