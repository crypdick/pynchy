"""Discord text support must not import dependencies from the voice extra."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - test invokes a fixed interpreter import probe.
import sys


def test_discord_channel_imports_without_voice_dependencies() -> None:
    probe = """
import builtins

original_import = builtins.__import__

def import_without_voice_dependencies(name, *args, **kwargs):
    if name.partition('.')[0] in {'davey', 'nacl'}:
        raise ImportError(f'blocked optional voice dependency: {name}')
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_voice_dependencies

from pynchy.plugins.channels.discord import DiscordChannel

assert DiscordChannel.__name__ == 'DiscordChannel'
"""
    subprocess.run(  # noqa: S603, RUF100 - fixed interpreter import-isolation probe.
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
