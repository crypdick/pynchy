"""Command classification for the bash security gate.

Three-tier cascade:
- SAFE: provably local commands (cannot reach network)
- NETWORK: known network-capable commands
- UNKNOWN: grey zone, requires Cop inspection when tainted
"""

from __future__ import annotations

import re
from enum import StrEnum

# Provably local — cannot reach the network regardless of arguments.
PROVABLY_LOCAL: frozenset[str] = frozenset({
    "awk", "base64", "basename", "bc", "cal", "cat", "column", "comm",
    "cut", "date", "df", "diff", "dirname", "du", "echo", "expand",
    "expr", "fd", "file", "find", "fmt", "fold", "free", "grep",
    "head", "hexdump", "id", "iconv", "jq", "less", "locale", "ls",
    "lscpu", "md5sum", "mktemp", "nl", "nproc", "od", "paste", "pwd",
    "readelf", "realpath", "rev", "rg", "sed", "seq", "sha256sum",
    "sort", "stat", "strings", "tac", "tail", "tr", "tree", "type",
    "uname", "unexpand", "uniq", "uptime", "wc", "which", "whoami",
    "xargs", "xxd",
})

# Known network-capable — single-token commands.
_NETWORK_SINGLE: frozenset[str] = frozenset({
    "curl", "wget", "nc", "netcat", "ncat", "telnet",
    "ssh", "scp", "sftp", "rsync",
    "nslookup", "dig", "host", "ping", "traceroute",
    "python", "python3", "node", "ruby", "perl", "php",
    # Shell builtins that can execute arbitrary (and therefore network) code.
    "eval",
})

# Known network-capable — multi-token prefixes (checked against full command).
_NETWORK_MULTI: tuple[str, ...] = (
    "apt-get install", "apt install",
    "pip install", "npm install", "yarn add", "cargo install",
    "bash -c", "sh -c",
)

# Regex for env-var prefix: VAR=value or VAR="value" before the real command.
_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S*\s+)+")

# Shell operators that separate commands in a pipeline/chain.
_SHELL_SPLIT = re.compile(r"\s*(?:\|\||&&|[|;]|\$\()\s*")


class CommandClass(StrEnum):
    SAFE = "safe"
    NETWORK = "network"
    UNKNOWN = "unknown"


def _extract_tokens(command: str) -> list[str]:
    """Extract the leading command token from each segment of a pipeline/chain.

    Handles:
    - env var prefixes: ``LC_ALL=C strings ...`` -> ``["strings"]``
    - pipelines: ``cat file | grep x`` -> ``["cat", "grep"]``
    - chains: ``echo hi && curl x`` -> ``["echo", "curl"]``
    - subshells: ``echo $(curl x)`` -> ``["echo", "curl"]``
    """
    segments = _SHELL_SPLIT.split(command)
    tokens: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # Strip env var prefixes (e.g. LC_ALL=C before the real command).
        seg = _ENV_PREFIX.sub("", seg).strip()
        if not seg:
            continue
        # First whitespace-delimited token is the command name.
        parts = seg.split()
        if parts:
            tokens.append(parts[0])
    return tokens


def classify_command(command: str) -> CommandClass:
    """Classify a bash command as SAFE, NETWORK, or UNKNOWN.

    Scans all segments of a pipeline/chain.  A single NETWORK segment
    makes the whole command NETWORK.  Only if *all* segments are SAFE is
    the command SAFE.  Otherwise UNKNOWN.
    """
    command = command.strip()
    if not command:
        return CommandClass.UNKNOWN

    # Check full command against multi-token network patterns first.
    cmd_lower = command.lower()
    for pattern in _NETWORK_MULTI:
        if pattern in cmd_lower:
            return CommandClass.NETWORK

    tokens = _extract_tokens(command)
    if not tokens:
        return CommandClass.UNKNOWN

    has_unknown = False
    for token in tokens:
        if token in _NETWORK_SINGLE:
            return CommandClass.NETWORK
        if token not in PROVABLY_LOCAL:
            has_unknown = True

    return CommandClass.UNKNOWN if has_unknown else CommandClass.SAFE
