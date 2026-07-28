"""Tests for Apple Container's writable Obsidian vault mirror."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from pynchy.host.learning.mirror import (
    automation_memory_dir,
    prepare_vault_mount_root,
    sync_vault_mount_mirror,
)
from pynchy.host.learning.paths import AutomationMemoryPaths, LearningPaths

if TYPE_CHECKING:
    from pathlib import Path


def _paths(vault_root: Path) -> LearningPaths:
    profile_root = vault_root / "systems/pynchy/profiles/research"
    return LearningPaths(
        profile="research",
        profile_slug="research",
        vault_root=vault_root,
        vault_mount_path="/workspace/vault",
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


def test_apple_vault_mirror_round_trips_profile_memory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    profile_note = vault / "systems/pynchy/profiles/research/memory/context.md"
    profile_note.parent.mkdir(parents=True)
    profile_note.write_text("profile context\n")
    paths = _paths(vault)
    with (
        patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True),
    ):
        mirror = prepare_vault_mount_root(paths)
        (mirror / "systems/pynchy/profiles/research/memory/new-note.md").write_text("learned\n")

        sync_vault_mount_mirror(paths)

    assert (
        vault / "systems/pynchy/profiles/research/memory/new-note.md"
    ).read_text() == "learned\n"


def test_automation_memory_round_trips_and_recovers_a_dirty_apple_mirror(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "vault/wiki/systems/pynchy/automation-memory/job-security"
    mirror = tmp_path / "data/learning/automation-memory-mirrors/job-security"
    dirty = mirror.parent / "job-security.dirty"
    paths = AutomationMemoryPaths(canonical=canonical, mirror=mirror, dirty_marker=dirty)
    canonical.mkdir(parents=True)
    (canonical / "ledger.json").write_text('{"source": "vault"}\n')

    with (
        patch(
            "pynchy.host.learning.mirror.resolve_automation_memory_paths",
            return_value=paths,
        ),
        patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True),
    ):
        with automation_memory_dir("job-security") as working:
            assert working == mirror
            (working / "ledger.json").write_text('{"source": "automation"}\n')

        assert (canonical / "ledger.json").read_text() == '{"source": "automation"}\n'
        assert not dirty.exists()

        (mirror / "ledger.json").write_text('{"source": "recovered"}\n')
        dirty.touch()
        with automation_memory_dir("job-security") as working:
            assert (working / "ledger.json").read_text() == '{"source": "recovered"}\n'

    assert (canonical / "ledger.json").read_text() == '{"source": "recovered"}\n'
