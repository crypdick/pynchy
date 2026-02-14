"""Ralph Wiggum loop — iterative agent execution with LLM verification.

Runs a worker agent in a loop:
  1. Worker agent executes the task prompt (in a container)
  2. Host runs check_command (e.g. pytest) in the group's worktree
  3. If check passes (exit 0) → done
  4. LLM verifier analyzes the check output and decides CONTINUE or STOP
  5. If CONTINUE → feed failure context back to worker, repeat from step 1
  6. If STOP or max iterations reached → halt

The verifier is a lightweight LLM call (not a full container agent) that
examines the check output and the iteration history to make a nuanced
stop/continue decision, including detecting stagnation.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pynchy.config import (
    GROUPS_DIR,
    MAIN_GROUP_FOLDER,
    PROJECT_ROOT,
    RALPH_CHECK_TIMEOUT,
)
from pynchy.logger import logger
from pynchy.types import RalphLoopConfig, RalphLoopState

# Active ralph loops, keyed by group_folder
_active_loops: dict[str, RalphLoopState] = {}

# Verifier prompt template — the LLM sees the check output and decides.
# Must respond with exactly CONTINUE or STOP on the first line, then reasoning.
VERIFIER_PROMPT = """\
You are a verification agent deciding whether an iterative coding task should continue or stop.

## Task being performed
{task_prompt}

## Check command
`{check_command}`

## Check command output (exit code {exit_code})
```
{check_output}
```

## Iteration {iteration} of {max_iterations}

## Recent check outputs (last {history_count} iterations)
{history}

## Instructions
Analyze the check output and decide:
- CONTINUE if the check is still failing AND progress is being made
  (errors are decreasing or changing)
- STOP if: the check passes, OR the same errors keep repeating
  with no progress, OR the errors look unfixable by the agent

Respond with exactly one of these on the FIRST line:
CONTINUE
STOP

Then on the following lines, briefly explain your reasoning (1-2 sentences).
"""


class RalphDeps(Protocol):
    """Dependencies for the Ralph loop, injected by the app."""

    async def run_worker_agent(
        self,
        group_folder: str,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None,
        extra_system_notices: list[str] | None,
    ) -> str: ...

    async def run_verifier(self, prompt: str) -> str: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def clear_session(self, group_folder: str) -> None: ...


def is_ralph_active(group_folder: str) -> bool:
    """Check if a Ralph loop is currently active for a group."""
    state = _active_loops.get(group_folder)
    return state is not None and state.active


def stop_ralph(group_folder: str) -> bool:
    """Signal an active Ralph loop to stop after the current iteration."""
    state = _active_loops.get(group_folder)
    if state and state.active:
        state.active = False
        logger.info("Ralph loop stop requested", group=group_folder)
        return True
    return False


def _get_check_cwd(group_folder: str) -> Path:
    """Get the working directory for running the check command."""
    from pynchy.config import WORKTREES_DIR

    # Use worktree if it exists, otherwise group dir, otherwise project root
    worktree = WORKTREES_DIR / group_folder
    if worktree.exists():
        return worktree

    if group_folder == MAIN_GROUP_FOLDER:
        return PROJECT_ROOT

    group_dir = GROUPS_DIR / group_folder
    if group_dir.exists():
        return group_dir

    return PROJECT_ROOT


async def _run_check_command(check_command: str, group_folder: str) -> tuple[int, str]:
    """Run the check command in the group's worktree. Returns (exit_code, output)."""
    cwd = _get_check_cwd(group_folder)

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            check_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=RALPH_CHECK_TIMEOUT,
            cwd=str(cwd),
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        # Truncate to avoid blowing up the verifier prompt
        if len(output) > 8000:
            output = output[:4000] + "\n\n... (truncated) ...\n\n" + output[-4000:]
        return result.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, f"Check command timed out after {RALPH_CHECK_TIMEOUT}s"
    except Exception as exc:
        return 1, f"Check command failed: {exc}"


def _is_stagnating(state: RalphLoopState) -> bool:
    """Detect if recent check outputs are identical (simple exact match)."""
    threshold = state.config.stagnation_threshold
    recent = state.recent_check_outputs
    if len(recent) < threshold:
        return False
    # Check if the last N outputs are all identical
    last_n = recent[-threshold:]
    return all(o == last_n[0] for o in last_n)


def _build_verifier_prompt(
    config: RalphLoopConfig,
    state: RalphLoopState,
    exit_code: int,
    check_output: str,
) -> str:
    """Build the prompt for the LLM verifier."""
    history_lines = []
    for i, output in enumerate(state.recent_check_outputs):
        # Show a compact summary of each previous iteration's check output
        truncated = output[:500] + "..." if len(output) > 500 else output
        history_lines.append(f"Iteration {i + 1}: {truncated}")
    history = "\n".join(history_lines) if history_lines else "(first iteration)"

    return VERIFIER_PROMPT.format(
        task_prompt=config.prompt,
        check_command=config.check_command,
        exit_code=exit_code,
        check_output=check_output,
        iteration=state.iteration,
        max_iterations=config.max_iterations,
        history_count=len(state.recent_check_outputs),
        history=history,
    )


def _parse_verifier_response(response: str) -> tuple[str, str]:
    """Parse verifier response into (decision, reasoning).

    Returns:
        ("CONTINUE", reasoning) or ("STOP", reasoning)
    """
    lines = response.strip().splitlines()
    if not lines:
        return "STOP", "Empty verifier response"

    first_line = lines[0].strip().upper()
    reasoning = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    if first_line == "CONTINUE":
        return "CONTINUE", reasoning
    # Default to STOP for any unexpected response
    return "STOP", reasoning or first_line


async def run_ralph_loop(
    config: RalphLoopConfig,
    group_folder: str,
    chat_jid: str,
    deps: RalphDeps,
) -> dict[str, Any]:
    """Run the Ralph Wiggum loop until completion or halt.

    Returns a summary dict with iteration count, final status, and reason.
    """
    state = RalphLoopState(
        config=config,
        group_folder=group_folder,
        chat_jid=chat_jid,
    )
    _active_loops[group_folder] = state

    logger.info(
        "Ralph loop started",
        group=group_folder,
        check_command=config.check_command,
        max_iterations=config.max_iterations,
    )

    await deps.broadcast_host_message(
        chat_jid,
        f"Ralph loop started (max {config.max_iterations} iterations, "
        f"check: `{config.check_command}`)",
    )

    final_status = "unknown"
    final_reason = ""

    try:
        for iteration in range(1, config.max_iterations + 1):
            state.iteration = iteration

            if not state.active:
                final_status = "stopped"
                final_reason = "Manually stopped"
                break

            # --- Step 1: Build worker prompt ---
            worker_messages = _build_worker_messages(config, state)

            system_notices = None
            if iteration > 1:
                # Give the worker context about the ralph loop
                system_notices = [
                    f"This is iteration {iteration} of a Ralph loop "
                    f"(max {config.max_iterations}). "
                    f"The check command `{config.check_command}` is still failing. "
                    "Fix the remaining issues, commit your work, and the check will be re-run."
                ]

            await deps.broadcast_host_message(
                chat_jid,
                f"Ralph iteration {iteration}/{config.max_iterations} — running worker agent...",
            )

            # --- Step 2: Run worker agent ---
            if config.session_mode == "fresh" and iteration > 1:
                await deps.clear_session(group_folder)

            agent_result = await deps.run_worker_agent(
                group_folder,
                chat_jid,
                worker_messages,
                None,  # on_output — let the app handle streaming
                system_notices,
            )

            if agent_result == "error":
                logger.warning(
                    "Ralph worker agent error",
                    group=group_folder,
                    iteration=iteration,
                )
                # Don't abort the loop — the check might still pass if a
                # previous iteration made partial progress
                await deps.broadcast_host_message(
                    chat_jid,
                    f"Ralph iteration {iteration} — worker agent error, running check anyway...",
                )

            if not state.active:
                final_status = "stopped"
                final_reason = "Manually stopped"
                break

            # --- Step 3: Run check command ---
            await deps.broadcast_host_message(
                chat_jid,
                f"Ralph iteration {iteration} — running check: `{config.check_command}`...",
            )

            exit_code, check_output = await _run_check_command(config.check_command, group_folder)

            logger.info(
                "Ralph check result",
                group=group_folder,
                iteration=iteration,
                exit_code=exit_code,
                output_len=len(check_output),
            )

            # --- Step 4: Check passed? ---
            if exit_code == 0:
                final_status = "success"
                final_reason = f"Check passed on iteration {iteration}"
                await deps.broadcast_host_message(
                    chat_jid,
                    f"Ralph loop complete — check passed on iteration {iteration}!",
                )
                break

            # Record this check output for stagnation detection
            state.recent_check_outputs.append(check_output)

            # --- Step 5: Quick stagnation check (before LLM call) ---
            if _is_stagnating(state):
                final_status = "stagnated"
                final_reason = (
                    f"Same check output for {config.stagnation_threshold} consecutive iterations"
                )
                await deps.broadcast_host_message(
                    chat_jid,
                    f"Ralph loop halted — stagnation detected after {iteration} iterations. "
                    f"Check output hasn't changed in {config.stagnation_threshold} iterations.",
                )
                break

            # --- Step 6: Ask LLM verifier ---
            if iteration < config.max_iterations:
                verifier_prompt = _build_verifier_prompt(config, state, exit_code, check_output)
                verifier_response = await deps.run_verifier(verifier_prompt)
                decision, reasoning = _parse_verifier_response(verifier_response)

                logger.info(
                    "Ralph verifier decision",
                    group=group_folder,
                    iteration=iteration,
                    decision=decision,
                    reasoning=reasoning[:200],
                )

                if decision == "STOP":
                    final_status = "verifier_stop"
                    final_reason = reasoning or "Verifier decided to stop"
                    await deps.broadcast_host_message(
                        chat_jid,
                        f"Ralph loop halted by verifier after {iteration} iterations: "
                        f"{reasoning[:200]}",
                    )
                    break

                await deps.broadcast_host_message(
                    chat_jid,
                    f"Ralph iteration {iteration} — verifier says CONTINUE: {reasoning[:200]}",
                )
        else:
            # Loop exhausted max_iterations
            final_status = "max_iterations"
            final_reason = f"Reached maximum of {config.max_iterations} iterations"
            await deps.broadcast_host_message(
                chat_jid,
                f"Ralph loop exhausted — reached {config.max_iterations} iterations "
                f"without passing check.",
            )
    except Exception as exc:
        final_status = "error"
        final_reason = str(exc)
        logger.error(
            "Ralph loop error",
            group=group_folder,
            iteration=state.iteration,
            error=str(exc),
        )
        await deps.broadcast_host_message(
            chat_jid,
            f"Ralph loop error on iteration {state.iteration}: {str(exc)[:200]}",
        )
    finally:
        _active_loops.pop(group_folder, None)

    summary = {
        "status": final_status,
        "reason": final_reason,
        "iterations": state.iteration,
        "max_iterations": config.max_iterations,
        "check_command": config.check_command,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    logger.info("Ralph loop finished", group=group_folder, **summary)
    return summary


def _build_worker_messages(
    config: RalphLoopConfig,
    state: RalphLoopState,
) -> list[dict]:
    """Build the message list for the worker agent."""
    ts = datetime.now(UTC).isoformat()

    messages = [
        {
            "message_type": "user",
            "sender": "ralph_loop",
            "sender_name": "Ralph Loop",
            "content": config.prompt,
            "timestamp": ts,
            "metadata": {"source": "ralph_loop", "iteration": state.iteration},
        }
    ]

    # After the first iteration, append the last check failure as context
    if state.recent_check_outputs:
        last_output = state.recent_check_outputs[-1]
        # Truncate for the message to the worker
        if len(last_output) > 4000:
            last_output = last_output[:2000] + "\n\n...(truncated)...\n\n" + last_output[-2000:]

        messages.append(
            {
                "message_type": "user",
                "sender": "ralph_loop",
                "sender_name": "Ralph Loop",
                "content": (
                    f"The check command `{config.check_command}` is still failing "
                    f"(iteration {state.iteration}/{config.max_iterations}).\n\n"
                    f"Check output:\n```\n{last_output}\n```\n\n"
                    "Please fix the remaining issues, then commit your work."
                ),
                "timestamp": ts,
                "metadata": {"source": "ralph_check_failure"},
            }
        )

    return messages
