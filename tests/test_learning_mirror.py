"""Tests for Apple Container's writable Obsidian vault mirror."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from pynchy.host.learning.mirror import prepare_vault_mount_root, sync_vault_mount_mirror
from pynchy.host.learning.paths import LearningPaths

if TYPE_CHECKING:
    from pathlib import Path


def _paths(vault_root: Path) -> LearningPaths:
    profile_root = vault_root / "systems/pynchy/profiles/research"
    return LearningPaths(
        profile="research",
        profile_slug="research",
        vault_root=vault_root,
        vault_mount_path="/workspace/vault",
        global_skills_root=vault_root / "systems/pynchy/skills",
        profile_root=profile_root,
        memory_root=profile_root / "memory",
        vault_mirror_root=vault_root.parent / "data" / "learning" / "vault-mirrors" / "research",
        host_vault_mirror_root=vault_root.parent
        / "data"
        / "learning"
        / "host-vault-mirrors"
        / "research",
        mounted_profile_root="/workspace/vault/systems/pynchy/profiles/research",
        mounted_memory_root="/workspace/vault/systems/pynchy/profiles/research/memory",
    )


def test_apple_vault_mirror_round_trips_global_learned_skills(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    profile_note = vault / "systems/pynchy/profiles/research/memory/context.md"
    existing_skill = vault / "systems/pynchy/skills/remember-routing/SKILL.md"
    profile_note.parent.mkdir(parents=True)
    existing_skill.parent.mkdir(parents=True)
    profile_note.write_text("profile context\n")
    existing_skill.write_text("---\nname: remember-routing\ntier: learned\n---\n")
    paths = _paths(vault)
    with (
        patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True),
    ):
        mirror = prepare_vault_mount_root(paths)
        mirrored_skill = mirror / "systems/pynchy/skills/remember-routing/SKILL.md"
        assert mirrored_skill.read_text() == existing_skill.read_text()

        new_skill = mirror / "systems/pynchy/skills/coordinate-reviews/SKILL.md"
        new_skill.parent.mkdir(parents=True)
        new_skill.write_text("---\nname: coordinate-reviews\ntier: learned\n---\n")
        (mirror / "systems/pynchy/profiles/research/memory/new-note.md").write_text("learned\n")

        sync_vault_mount_mirror(paths)

    assert (vault / "systems/pynchy/skills/coordinate-reviews/SKILL.md").read_text() == (
        "---\nname: coordinate-reviews\ntier: learned\n---\n"
    )
    assert (
        vault / "systems/pynchy/profiles/research/memory/new-note.md"
    ).read_text() == "learned\n"
