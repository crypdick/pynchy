"""Deny imperative production infrastructure changes from coding-agent shells."""

from __future__ import annotations

import json
import re
import shlex
import sys
from itertools import pairwise
from pathlib import PurePath
from typing import TextIO

_SHELL_OPERATORS = frozenset({"&", "&&", "|", "||", ";", ";;", "(", ")"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_WRAPPERS = frozenset({"command", "env", "sudo"})
_SHELLS = frozenset({"bash", "dash", "sh", "zsh"})
_SSH_OPTIONS_WITH_VALUES = frozenset(
    {
        "-B",
        "-b",
        "-c",
        "-D",
        "-E",
        "-e",
        "-F",
        "-I",
        "-i",
        "-J",
        "-L",
        "-l",
        "-m",
        "-O",
        "-o",
        "-P",
        "-p",
        "-Q",
        "-R",
        "-S",
        "-W",
        "-w",
    }
)
_KUBECTL_GLOBAL_OPTIONS_WITH_VALUES = frozenset(
    {
        "--as",
        "--as-group",
        "--cache-dir",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--cluster",
        "--context",
        "--kubeconfig",
        "--namespace",
        "--request-timeout",
        "--server",
        "--token",
        "--user",
        "-n",
        "-s",
    }
)
_KUBECTL_IMPERATIVE_VERBS = frozenset({"edit", "patch", "replace", "scale"})
_KUBECTL_IMPERATIVE_SETTERS = frozenset(
    {"env", "image", "resources", "selector", "serviceaccount", "subject"}
)
_KUBECTL_PERSISTENT_CREATE_TYPES = frozenset(
    {
        "clusterrole",
        "clusterrolebinding",
        "configmap",
        "cronjob",
        "deployment",
        "ingress",
        "namespace",
        "persistentvolume",
        "persistentvolumeclaim",
        "priorityclass",
        "role",
        "rolebinding",
        "secret",
        "service",
        "serviceaccount",
        "statefulset",
    }
)
_SHELL_TOOL_NAMES = frozenset({"Bash", "exec_command", "shell"})
_DENIAL_PREFIX = "Blocked by IaC guard"


def blocked_infrastructure_operation(command: str) -> str | None:
    """Return a secret-safe denial reason for an imperative infrastructure command."""
    operation = _blocked_operation(command)
    if operation is None:
        return None
    return (
        f"{_DENIAL_PREFIX}: {operation} changes runtime state outside tracked "
        "infrastructure as code. Edit the tracked deployment source and use its "
        "reconciler or declarative apply path."
    )


def _blocked_operation(command: str, *, depth: int = 0) -> str | None:
    if depth > 4:
        return None
    for words in _command_segments(command):
        argv = _unwrap(words)
        if not argv:
            continue
        executable = _basename(argv[0])
        arguments = argv[1:]

        if executable == "ssh":
            remote = _ssh_remote_command(arguments)
            if remote and (blocked := _blocked_operation(remote, depth=depth + 1)):
                return blocked
            continue
        if executable in _SHELLS and (nested := _shell_command(arguments)):
            if blocked := _blocked_operation(nested, depth=depth + 1):
                return blocked
            continue
        if blocked := _blocked_local_operation(executable, arguments):
            return blocked
    return None


def _blocked_local_operation(executable: str, arguments: list[str]) -> str | None:
    if executable == "k3s" and arguments:
        executable = _basename(arguments[0])
        arguments = arguments[1:]
    if executable == "kubectl":
        return _blocked_kubectl(arguments)
    if executable == "ctr" and _contains_pair(arguments, "images", "import"):
        return "ctr images import"
    if executable in {"docker", "nerdctl", "podman"} and (
        _contains_pair(arguments, "image", "load") or _first_word(arguments) == "load"
    ):
        return f"{executable} image load"
    return None


def _command_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _unwrap(words: list[str]) -> list[str]:
    index = 0
    while index < len(words):
        while index < len(words) and _ASSIGNMENT.fullmatch(words[index]):
            index += 1
        if index >= len(words) or _basename(words[index]) not in _WRAPPERS:
            break
        wrapper = _basename(words[index])
        index += 1
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            index += 1
            if wrapper == "sudo" and option in {"-C", "-D", "-g", "-h", "-p", "-R", "-T", "-u"}:
                index += 1
        if wrapper == "env":
            while index < len(words) and _ASSIGNMENT.fullmatch(words[index]):
                index += 1
    return words[index:]


def _ssh_remote_command(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if not argument.startswith("-"):
            break
        index += 1
        if argument in _SSH_OPTIONS_WITH_VALUES:
            index += 1
    if index >= len(arguments):
        return None
    return " ".join(arguments[index + 1 :]) or None


def _shell_command(arguments: list[str]) -> str | None:
    for index, argument in enumerate(arguments[:-1]):
        if argument in {"-c", "-lc"}:
            return arguments[index + 1]
    return None


def _blocked_kubectl(arguments: list[str]) -> str | None:
    verb, remainder = _kubectl_subcommand(arguments)
    if verb in _KUBECTL_IMPERATIVE_VERBS:
        return f"kubectl {verb}"
    if verb == "set":
        setter = _first_word(remainder)
        if setter in _KUBECTL_IMPERATIVE_SETTERS:
            return f"kubectl set {setter}"
    if verb == "create":
        resource = _first_word(remainder)
        if resource in _KUBECTL_PERSISTENT_CREATE_TYPES:
            return f"kubectl create {resource}"
    return None


def _kubectl_subcommand(arguments: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if not argument.startswith("-"):
            return argument.casefold(), arguments[index + 1 :]
        index += 1
        if "=" not in argument and argument in _KUBECTL_GLOBAL_OPTIONS_WITH_VALUES:
            index += 1
    if index < len(arguments):
        return arguments[index].casefold(), arguments[index + 1 :]
    return None, []


def _contains_pair(arguments: list[str], first: str, second: str) -> bool:
    normalized = [_basename(argument).casefold() for argument in arguments]
    return any(left == first and right == second for left, right in pairwise(normalized))


def _first_word(arguments: list[str]) -> str | None:
    return next(
        (argument.casefold() for argument in arguments if not argument.startswith("-")),
        None,
    )


def _basename(value: str) -> str:
    return PurePath(value).name.casefold()


def _payload_tool(payload: dict[str, object]) -> tuple[str, dict[str, object]]:
    nested = payload.get("tool")
    tool = nested if isinstance(nested, dict) else {}
    name = payload.get("tool_name") or payload.get("toolName") or tool.get("name") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or tool.get("input") or {}
    return str(name), tool_input if isinstance(tool_input, dict) else {}


def _emit_deny(stdout: TextIO, reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        stdout,
    )


def run_hook(stdin: TextIO, stdout: TextIO) -> int:
    """Evaluate one Codex- or Claude-shaped PreToolUse request."""
    try:
        payload = json.load(stdin)
    except (json.JSONDecodeError, OSError):
        _emit_deny(stdout, f"{_DENIAL_PREFIX}: malformed hook input; failing closed.")
        return 0
    if not isinstance(payload, dict):
        _emit_deny(stdout, f"{_DENIAL_PREFIX}: malformed hook input; failing closed.")
        return 0

    tool_name, tool_input = _payload_tool(payload)
    if tool_name not in _SHELL_TOOL_NAMES:
        return 0
    command = tool_input.get("command") or tool_input.get("cmd")
    if not isinstance(command, str):
        _emit_deny(stdout, f"{_DENIAL_PREFIX}: shell command is missing; failing closed.")
        return 0
    if denial := blocked_infrastructure_operation(command):
        _emit_deny(stdout, denial)
    return 0


def main() -> int:
    return run_hook(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
