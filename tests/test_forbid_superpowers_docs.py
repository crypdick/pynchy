from scripts.prek_hooks.forbid_superpowers_docs import blocked_paths


def test_blocked_paths_rejects_only_docs_superpowers() -> None:
    assert blocked_paths(["docs/superpowers/plan.md", "docs/architecture/index.md"]) == [
        "docs/superpowers/plan.md"
    ]


def test_blocked_paths_accepts_similarly_named_paths() -> None:
    assert blocked_paths(["superpowers/plan.md", "docs/superpowers-old/plan.md"]) == []
