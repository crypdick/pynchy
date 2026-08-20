"""Command classification for the bash security gate.

Three-tier cascade:
- SAFE: provably local commands (cannot reach network)
- NETWORK: known network-capable commands
- UNKNOWN: grey zone, requires Cop inspection when tainted
"""

from __future__ import annotations

import re
import shlex
from enum import StrEnum

# Provably local — cannot reach the network regardless of arguments.
PROVABLY_LOCAL: frozenset[str] = frozenset(
    {
        "awk",
        "base64",
        "basename",
        "bc",
        "cal",
        "cat",
        "column",
        "comm",
        "cut",
        "date",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "expand",
        "expr",
        "fd",
        "file",
        "find",
        "fmt",
        "fold",
        "free",
        "grep",
        "head",
        "hexdump",
        "id",
        "iconv",
        "jq",
        "less",
        "locale",
        "ls",
        "lscpu",
        "md5sum",
        "mktemp",
        "nl",
        "nproc",
        "od",
        "paste",
        "pwd",
        "printf",
        "readelf",
        "realpath",
        "rev",
        "rg",
        "sed",
        "seq",
        "sha256sum",
        "sort",
        "stat",
        "strings",
        "tac",
        "tail",
        "tr",
        "tree",
        "type",
        "uname",
        "unexpand",
        "uniq",
        "uptime",
        "wc",
        "which",
        "whoami",
        "xargs",
        "xxd",
    }
)

# Known network-capable — single-token commands.
_NETWORK_SINGLE: frozenset[str] = frozenset(
    {
        "curl",
        "gh",
        "wget",
        "nc",
        "netcat",
        "ncat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nslookup",
        "dig",
        "host",
        "ping",
        "traceroute",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
        # Shell builtins that can execute arbitrary (and therefore network) code.
        "eval",
    }
)

# Known network-capable — multi-token prefixes (checked against full command).
_NETWORK_MULTI: tuple[str, ...] = (
    "apt-get install",
    "apt install",
    "pip install",
    "npm install",
    "yarn add",
    "cargo install",
    "git clone",
    "git fetch",
    "git ls-remote",
    "git pull",
    "git push",
    "git submodule",
    "bash -c",
    "sh -c",
)

# Regex for env-var prefix: VAR=value or VAR="value" before the real command.
_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S*\s+)+")

# Shell operators that separate commands in a pipeline/chain.
_SHELL_SPLIT = re.compile(r"\s*(?:\|\||&&|[|;]|\$\()\s*")
_TRUSTED_SHELL_WRAPPER = "/bin/bash"


class CommandClass(StrEnum):
    SAFE = "safe"
    NETWORK = "network"
    UNKNOWN = "unknown"


def _unwrap_shell_wrapper(command: str) -> str | None:
    """Return script passed through one trusted runner shell wrapper."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) == 3 and parts[0] == _TRUSTED_SHELL_WRAPPER and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return None


def classify_command(command: str) -> CommandClass:
    """Classify a bash command as SAFE, NETWORK, or UNKNOWN.

    Scans all segments of a pipeline/chain.  A single NETWORK segment
    makes the whole command NETWORK.  Only if *all* segments are SAFE is
    the command SAFE.  Otherwise UNKNOWN.
    """
    command = command.strip()
    if not command:
        return CommandClass.UNKNOWN

    # Codex wraps every shell call in /bin/bash -lc. Classify the owned inner
    # script; treating the runner wrapper itself as unknown sends all commands
    # to Cop and defeats the local classifier.
    inner_command = _unwrap_shell_wrapper(command)
    if inner_command is not None:
        return classify_command(inner_command)

    # Check full command against multi-token network patterns first.
    cmd_lower = command.lower()
    for pattern in _NETWORK_MULTI:
        if pattern in cmd_lower:
            return CommandClass.NETWORK

    segments = _SHELL_SPLIT.split(command)
    has_unknown = False
    found_command = False
    for segment in segments:
        stripped_segment = _ENV_PREFIX.sub("", segment.strip()).strip()
        if not stripped_segment:
            continue
        found_command = True
        command_name = stripped_segment.split()[0]
        if command_name in _NETWORK_SINGLE:
            return CommandClass.NETWORK
        if command_name not in PROVABLY_LOCAL:
            has_unknown = True

    return CommandClass.UNKNOWN if has_unknown or not found_command else CommandClass.SAFE
