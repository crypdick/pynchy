"""Tests for Apple Container's writable Obsidian vault mirror."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from pynchy.host.learning.api import (
    automation_memory_dir,
    prepare_full_vault_host_root,
    prepare_vault_mount_root,
    sync_automation_memory,
)
from pynchy.host.learning.mirror import (
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


def test_non_apple_learning_mounts_use_the_canonical_vault(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "vault")
    mirror_note = paths.vault_mirror_root / "systems/pynchy/profiles/research/note.md"
    mirror_note.parent.mkdir(parents=True)
    mirror_note.write_text("must not sync\n")

    with patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=False):
        assert prepare_vault_mount_root(paths) == paths.vault_root
        assert prepare_full_vault_host_root(paths) == paths.vault_root
        sync_vault_mount_mirror(paths)

    assert not (paths.profile_root / "note.md").exists()


def test_apple_host_review_requires_a_prepared_full_vault_mirror(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "vault")

    with patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True):
        assert prepare_full_vault_host_root(paths) is None

        paths.host_vault_mirror_root.mkdir(parents=True)

        assert prepare_full_vault_host_root(paths) == paths.host_vault_mirror_root


def test_apple_vault_mirror_creates_an_empty_profile_mount_when_source_is_absent(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path / "vault")

    with patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True):
        sync_vault_mount_mirror(paths)
        mirror = prepare_vault_mount_root(paths)

    assert mirror == paths.vault_mirror_root
    assert (mirror / "systems/pynchy/profiles/research").is_dir()
    assert not paths.profile_root.exists()


def test_automation_memory_is_unavailable_when_learning_paths_are_not_configured() -> None:
    with (
        patch("pynchy.host.learning.mirror.resolve_automation_memory_paths", return_value=None),
        automation_memory_dir("job-security") as working,
    ):
        assert working is None


def test_non_apple_automation_memory_writes_directly_to_canonical_storage(tmp_path: Path) -> None:
    canonical = tmp_path / "vault/wiki/systems/pynchy/automation-memory/job-security"
    mirror = tmp_path / "data/learning/automation-memory-mirrors/job-security"
    paths = AutomationMemoryPaths(
        canonical=canonical,
        mirror=mirror,
        dirty_marker=mirror.parent / "job-security.dirty",
    )

    with (
        patch(
            "pynchy.host.learning.mirror.resolve_automation_memory_paths",
            return_value=paths,
        ),
        patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=False),
    ):
        with automation_memory_dir("job-security") as working:
            assert working == canonical
            (working / "note.md").write_text("canonical\n")

        sync_automation_memory("job-security")

    assert (canonical / "note.md").read_text() == "canonical\n"
    assert not mirror.exists()


def test_apple_automation_memory_creates_canonical_storage_after_a_new_write(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "vault/wiki/systems/pynchy/automation-memory/job-security"
    mirror = tmp_path / "data/learning/automation-memory-mirrors/job-security"
    paths = AutomationMemoryPaths(
        canonical=canonical,
        mirror=mirror,
        dirty_marker=mirror.parent / "job-security.dirty",
    )

    with (
        patch(
            "pynchy.host.learning.mirror.resolve_automation_memory_paths",
            return_value=paths,
        ),
        patch("pynchy.host.learning.mirror.should_use_vault_mount_mirror", return_value=True),
        automation_memory_dir("job-security") as working,
    ):
        (working / "note.md").write_text("new mirror note\n")

    assert (canonical / "note.md").read_text() == "new mirror note\n"
    assert not paths.dirty_marker.exists()
