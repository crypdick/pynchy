"""Keep publication host-managed while allowing agents to resolve sync conflicts."""

from __future__ import annotations

import re
import shlex
from typing import Any

from agent_runner.hooks import HookDecision

_GIT_REBASE_RECOVERY = frozenset({"--continue", "--abort", "--skip"})
_RAW_HOST_REPO_MOUNT = "/danger/raw-host-repos/"
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_SHELL_LAUNCHERS = frozenset({"bash", "dash", "sh", "zsh"})
# Git resolves these builtins before looking up aliases. Any other subcommand
# could expand an alias whose body bypasses this command-level guard.
_GIT_BUILTIN_OPERATIONS = frozenset(
    [
        *"""
        add am annotate apply archive backfill bisect blame branch bugreport bundle cat-file
        check-attr check-ignore check-mailmap check-ref-format checkout checkout-index cherry
        cherry-pick clean clone column commit commit-graph commit-tree config count-objects
        credential credential-cache credential-store cvsimport cvsexportcommit cvsserver daemon
        describe diagnose diff diff-files diff-index diff-pairs diff-tree difftool fast-export
        fast-import fetch fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo
        format-patch fsck gc get-tar-commit-id grep gui hash-object help hook imap-send index-pack
        init instaweb interpret-trailers log ls-files ls-remote ls-tree mailinfo mailsplit
        maintenance merge merge-base merge-file merge-index merge-one-file merge-tree mergetool
        mktag mktree multi-pack-index mv name-rev notes pack-objects pack-redundant pack-refs
        patch-id p4 prune prune-packed pull push range-diff read-tree rebase reflog remote repack
        replay request-pull reset restore revert rev-list rev-parse rm scalar send-email show
        show-branch show-index show-ref shortlog sparse-checkout split-index stash status stripspace
        submodule symbolic-ref tag unpack-file unpack-objects update-index update-ref
        update-server-info var verify-commit verify-pack verify-tag version whatchanged worktree
        write-tree
        """.split(),  # noqa: SIM905 - grouped builtins are easier to audit than a 140-item literal.
        "replace",  # temporal-ok - Git builtin name.
        "switch",  # temporal-ok - Git builtin name.
    ]
)
_GIT_OPTIONS_WITH_VALUES = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)
_UV_OPTIONS_WITH_VALUES = frozenset(
    {
        "--directory",
        "--env-file",
        "--from",
        "--project",
        "--python",
        "--with",
        "--with-editable",
    }
)
_PYTHON_EXECUTABLE = re.compile(r"python(?:\d+(?:\.\d+)*)?$")

_REASON = (
    "Direct git push/pull or starting a rebase is blocked. Use the sync_worktree_to_main "
    "tool instead — it coordinates with the host to publish your changes as a pull request. "
    "You may resolve a conflict created by that tool with git rebase "
    "--continue, --abort, or --skip."
)
_WORKTREE_REASON = (
    "Do not work in the raw host checkout mount. Use the isolated worktree at "
    "/home/agent/src/<owner>/<repo> instead; Pynchy coordinates publication through "
    "sync_worktree_to_main."
)
_PERSONALIZATION_REASON = (
    "pynchy publish-personalization is a host operator command. Agents cannot publish "
    "the independent personalization repository."
)
_DYNAMIC_SHELL_REASON = (
    "Shell command substitution, dynamic command names, eval, env -S, and shell scripts are "
    "blocked because generated commands and script content cannot be safely inspected."
)


def _strip_redirections(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Remove shell redirections so their operands cannot obscure a command."""
    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if _is_redirection_operator(token):
            if cleaned and cleaned[-1].isdigit():
                cleaned.pop()
            index += 2
            continue
        cleaned.append(token)
        index += 1
    return tuple(cleaned)


def _is_redirection_operator(token: str) -> bool:
    return bool(token) and ("<" in token or ">" in token)


def _shell_commands(command: str) -> tuple[tuple[str, ...], ...]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True  # noqa: V101
        tokens = tuple(lexer)
    except ValueError:
        return ()

    commands: list[tuple[str, ...]] = []
    current: list[str] = []
    for part in tokens:
        if part in _SHELL_SEPARATORS:
            if current:
                commands.append(_strip_redirections(tuple(current)))
                current = []
            continue
        current.append(part)
    if current:
        commands.append(_strip_redirections(tuple(current)))
    return tuple(commands)


def _contains_dynamic_shell_expansion(command: str) -> bool:
    """Recognize command substitution while leaving single-quoted text alone."""
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "\\" and not single_quoted:
            escaped = True
        elif char == "'" and not double_quoted:
            single_quoted = not single_quoted
        elif char == '"' and not single_quoted:
            double_quoted = not double_quoted
        elif not single_quoted and (
            char == "`" or (char == "$" and index + 1 < len(command) and command[index + 1] == "(")
        ):
            return True
    return False


def _executable_name(token: str) -> str:
    return token.rsplit("/", maxsplit=1)[-1]


def _strip_environment_prefix(argv: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(argv) and "=" in argv[index] and not argv[index].startswith("="):
        index += 1
    if index >= len(argv) or _executable_name(argv[index]) != "env":
        return argv[index:]

    index += 1
    while index < len(argv):
        part = argv[index]
        if part == "-u" and index + 1 < len(argv):
            index += 2
        elif part.startswith("-") or ("=" in part and not part.startswith("=")):
            index += 1
        else:
            break
    return argv[index:]


def _uv_run_argv(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    executable = _executable_name(argv[0]) if argv else ""
    if executable == "uv" and len(argv) > 1 and argv[1] == "run":
        index = 2
    elif executable == "uvx":
        index = 1
    else:
        return None
    while index < len(argv):
        part = argv[index]
        if part == "--":
            index += 1
            break
        if not part.startswith("-"):
            return argv[index:]
        if part in _UV_OPTIONS_WITH_VALUES:
            index += 2
        else:
            index += 1
    return argv[index:] if index < len(argv) else None


def _strip_command_prefix(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Remove the shell's ``command`` wrapper before inspecting its target."""
    argv = _strip_environment_prefix(argv)
    if not argv or _executable_name(argv[0]) != "command":
        return argv
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        index += 1
    return argv[index:]


def _skip_launcher_options(
    argv: tuple[str, ...],
    index: int,
    options_with_values: set[str],
) -> int:
    """Skip options owned by a fixed command launcher."""
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        index += 1
        if option in options_with_values:
            index += 1
    return index


def _strip_shell_launchers(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Remove known launchers while retaining the argv they execute."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if "=" in token and not token.startswith("="):
            index += 1
            continue
        executable = _executable_name(token)
        if executable in {"!", "builtin", "do", "else", "then"}:
            index += 1
            continue
        if executable == "command":
            index = _skip_launcher_options(argv, index + 1, set())
            continue
        if executable == "exec":
            index = _skip_launcher_options(argv, index + 1, {"-a"})
            continue
        if executable == "time":
            index = _skip_launcher_options(argv, index + 1, set())
            continue
        if executable == "nice":
            index = _skip_launcher_options(argv, index + 1, {"-n", "--adjustment"})
            continue
        if executable == "timeout":
            index = _skip_launcher_options(
                argv,
                index + 1,
                {"-k", "--kill-after", "-s", "--signal"},
            )
            if index < len(argv):
                index += 1  # timeout duration
            if index < len(argv) and argv[index] == "--":
                index += 1
            continue
        if executable == "nohup":
            index = _skip_launcher_options(argv, index + 1, set())
            continue
        return argv[index:]
    return ()


def _first_command_word(argv: tuple[str, ...]) -> str | None:
    """Return command word after assignments and supported shell launchers."""
    argv = _strip_command_prefix(_strip_shell_launchers(argv))
    return argv[0] if argv else None


def _uses_dynamic_command_name(argv: tuple[str, ...]) -> bool:
    command = _first_command_word(argv)
    return command is not None and "$" in command


def _uses_dynamic_protected_subcommand(argv: tuple[str, ...]) -> bool:
    """Reject variable-selected Git or Pynchy subcommands, but allow argument paths."""
    argv = _strip_command_prefix(_strip_shell_launchers(argv))
    if not argv:
        return False
    executable = _executable_name(argv[0])
    if executable == "pynchy":
        return len(argv) > 1 and "$" in argv[1]
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        return (
            len(argv) > 2
            and argv[1] == "-m"
            and (
                "$" in argv[2]
                or (len(argv) > 3 and argv[2] in {"pynchy", "pynchy.__main__"} and "$" in argv[3])
            )
        )
    nested = _uv_run_argv(argv)
    if nested is not None:
        return _uses_dynamic_protected_subcommand(nested)
    operation = _git_operation(argv)
    return operation is not None and "$" in operation[0]


def _is_host_personalization_publish(argv: tuple[str, ...]) -> bool:
    argv = _strip_command_prefix(argv)
    if not argv:
        return False
    executable = _executable_name(argv[0])
    if executable == "pynchy":
        return len(argv) > 1 and argv[1] == "publish-personalization"
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        return (
            len(argv) > 3
            and argv[1] == "-m"
            and argv[2] in {"pynchy", "pynchy.__main__"}
            and argv[3] == "publish-personalization"
        )
    nested = _uv_run_argv(argv)
    return nested is not None and _is_host_personalization_publish(nested)


def _contains_host_personalization_publish(argv: tuple[str, ...]) -> bool:
    """Detect the host-only command after shell control words or wrappers."""
    return any(_is_host_personalization_publish(argv[index:]) for index in range(len(argv)))


def _git_operation(argv: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    argv = _strip_command_prefix(argv)
    for start, token in enumerate(argv):
        if _executable_name(token) != "git":
            continue
        index = start + 1
        while index < len(argv) and argv[index].startswith("-"):
            if argv[index] in _GIT_OPTIONS_WITH_VALUES:
                index += 2
            else:
                index += 1
        if index < len(argv):
            return argv[index], argv[index + 1 :]
    return None


def _starts_new_rebase(argv: tuple[str, ...]) -> bool:
    operation = _git_operation(argv)
    if operation is None or operation[0] != "rebase":
        return False
    arguments = operation[1]
    return not arguments or arguments[0] not in _GIT_REBASE_RECOVERY


def _is_blocked_git_publication(argv: tuple[str, ...]) -> bool:
    operation = _git_operation(argv)
    return operation is not None and operation[0] in {"pull", "push"}


def _is_git_alias_invocation(argv: tuple[str, ...]) -> bool:
    operation = _git_operation(argv)
    return operation is not None and operation[0] not in _GIT_BUILTIN_OPERATIONS


def _first_non_option(arguments: tuple[str, ...]) -> str | None:
    return next((argument for argument in arguments if not argument.startswith("-")), None)


def _git_runs_command(argv: tuple[str, ...]) -> bool:
    operation = _git_operation(argv)
    if operation is None:
        return False
    name, arguments = operation
    return (
        name in {"difftool", "filter-branch", "for-each-repo"}
        or (name == "bisect" and _first_non_option(arguments) == "run")
        or (name == "submodule" and _first_non_option(arguments) == "foreach")
    )


def _uses_environment_split(argv: tuple[str, ...]) -> bool:
    argv = _strip_shell_launchers(argv)
    index = 0
    while index < len(argv) and "=" in argv[index] and not argv[index].startswith("="):
        index += 1
    if index >= len(argv) or _executable_name(argv[index]) != "env":
        return False

    index += 1
    while index < len(argv):
        part = argv[index]
        if part in {"-S", "--split-string"} or part.startswith(("-S", "--split-string=")):
            return True
        if part in {"-u", "--unset"}:
            index += 2
        elif part.startswith("-") or ("=" in part and not part.startswith("=")):
            index += 1
        else:
            return False
    return False


def _sources_script(argv: tuple[str, ...]) -> bool:
    argv = _strip_command_prefix(argv)
    while argv and _executable_name(argv[0]) in {
        "!",
        "builtin",
        "do",
        "else",
        "exec",
        "then",
        "time",
    }:
        argv = argv[1:]
    return bool(argv) and _executable_name(argv[0]) in {".", "source"}


def _shell_runs_script(argv: tuple[str, ...]) -> bool:
    for start, token in enumerate(argv):
        if _executable_name(token) not in _SHELL_LAUNCHERS:
            continue
        for argument in argv[start + 1 :]:
            if argument == "-c" or (argument.startswith("-") and "c" in argument[1:]):
                return False
            if not argument.startswith("-"):
                return True
        return True
    return False


def _is_eval_invocation(argv: tuple[str, ...]) -> bool:
    argv = _strip_command_prefix(argv)
    while argv and _executable_name(argv[0]) in {"!", "builtin", "do", "else", "then", "time"}:
        argv = argv[1:]
    return bool(argv) and _executable_name(argv[0]) == "eval"


def _nested_shell_command_texts(argv: tuple[str, ...]) -> tuple[str, ...]:
    argv = _strip_command_prefix(argv)
    commands: list[str] = []
    for start, token in enumerate(argv):
        if _executable_name(token) not in _SHELL_LAUNCHERS:
            continue
        for index, part in enumerate(argv[start + 1 :], start=start + 1):
            if part == "-c" or (part.startswith("-") and "c" in part[1:]):
                if index + 1 < len(argv):
                    commands.append(argv[index + 1])
                break
    return tuple(commands)


def _nested_shell_commands(argv: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    command_texts = _nested_shell_command_texts(argv)
    return tuple(nested for command in command_texts for nested in _shell_commands(command))


def _shell_command_text_variants(command: str) -> tuple[str, ...]:
    """Return raw nested shell command text so dynamic syntax stays visible."""
    children = tuple(
        child for argv in _shell_commands(command) for child in _nested_shell_command_texts(argv)
    )
    return (
        command,
        *(nested for child in children for nested in _shell_command_text_variants(child)),
    )


def _command_variants(argv: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Return raw argv, normalized target, and recursively parsed launcher targets."""
    normalized = _strip_command_prefix(argv)
    if not normalized:
        return (argv,)
    children = _nested_shell_commands(normalized)
    uv_target = _uv_run_argv(normalized)
    if uv_target is not None:
        children = (*children, uv_target)
    return (
        argv,
        normalized,
        *(nested for child in children for nested in _command_variants(child)),
    )


async def guard_git_hook(  # noqa: RUF029 - async hook API.
    tool_name: str,
    tool_input: dict[str, Any],
) -> HookDecision:
    """Block direct publication and rebase initiation while permitting recovery."""
    if tool_name != "Bash":
        return HookDecision(allowed=True)

    command = tool_input.get("command", "")
    if _RAW_HOST_REPO_MOUNT in command:
        return HookDecision(allowed=False, reason=_WORKTREE_REASON)
    commands = _shell_commands(command)
    variants = tuple(variant for argv in commands for variant in _command_variants(argv))
    command_texts = _shell_command_text_variants(command)
    has_dynamic_expansion = any(_contains_dynamic_shell_expansion(text) for text in command_texts)
    has_dynamic_execution = has_dynamic_expansion or any(
        _is_eval_invocation(argv)
        or _uses_environment_split(argv)
        or _sources_script(argv)
        or _shell_runs_script(argv)
        or _uses_dynamic_command_name(argv)
        or _uses_dynamic_protected_subcommand(argv)
        for argv in variants
    )
    if has_dynamic_execution:
        return HookDecision(allowed=False, reason=_DYNAMIC_SHELL_REASON)
    if any(_contains_host_personalization_publish(argv) for argv in variants):
        return HookDecision(allowed=False, reason=_PERSONALIZATION_REASON)
    if any(
        _is_blocked_git_publication(argv)
        or _starts_new_rebase(argv)
        or _is_git_alias_invocation(argv)
        or _git_runs_command(argv)
        for argv in variants
    ):
        return HookDecision(allowed=False, reason=_REASON)

    return HookDecision(allowed=True)
