"""Durable task retirement for terminal Linear conversations."""

from __future__ import annotations

from functools import partial

from pynchy.conversation.api import (
    Conversation,
    ConversationId,
    ConversationSubjectKey,
)
from pynchy.host.orchestrator.temporal.schedules import (
    agent_task_schedule_id,
    agent_task_workflow_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
)
from pynchy.host.orchestrator.temporal.workflow_control import cancel_scheduled_agent_workflow
from pynchy.host.orchestrator.webhook_terminal_retirement import (
    TerminalConversationRetirementDeps,
    retire_terminal_runtime,
)
from pynchy.identifiers import GroupFolder
from pynchy.scheduling.api import (
    ScheduledTask,
    agent_task_occurrence_workflow_id,
)
from pynchy.state.api import (
    cancel_task_and_checkpoint,
    clear_in_flight_turn,
    delete_host_job,
    get_conversation,
    get_conversation_for_subject_key,
    get_host_job_by_id,
    get_task_by_id,
    get_tasks_for_conversation,
    get_unfinished_work_item_execution,
    get_work_item_execution_for_task,
    retire_latest_terminal_work_item_conversation,
    retire_terminal_execution_resources_if_unowned,
)
from pynchy.work_items.api import (
    WorkItemExecution,
)


async def cancel_scheduled_task(task_id: str) -> None:
    """Cancel active execution before retiring one scheduled task checkpoint."""
    task = await get_task_by_id(task_id)
    if task is None:
        return
    workflow_ids = [
        agent_task_workflow_id(task)
        if task.schedule_type == "once"
        else f"{agent_task_schedule_id(task)}-workflow"
    ]
    if (
        task.schedule_type == "once"
        and task.superseded_occurrence_due_at is not None
        and task.superseded_occurrence_generation is not None
    ):
        superseded_id = agent_task_occurrence_workflow_id(
            task.id,
            task.superseded_occurrence_due_at,
            task.superseded_occurrence_generation,
        )
        if superseded_id not in workflow_ids:
            workflow_ids.append(superseded_id)
    for workflow_id in workflow_ids:
        await cancel_scheduled_agent_workflow(workflow_id)
    await cancel_task_and_checkpoint(task_id)


async def cancel_scheduled_host_job(job_id: str) -> None:
    """Cancel active execution before deleting one durable host job."""
    job = await get_host_job_by_id(job_id)
    if job is None:
        return
    workflow_id = (
        database_host_job_workflow_id(job)
        if job.schedule_type == "once"
        else f"{database_host_job_schedule_id(job)}-workflow"
    )
    await cancel_scheduled_agent_workflow(workflow_id)
    await delete_host_job(job_id)


async def retire_conversation_tasks(conversation_id: ConversationId) -> None:
    """Cancel active workflows before retiring their durable task checkpoints."""
    conversation = await get_conversation(conversation_id)
    if conversation is None:
        raise RuntimeError(f"Terminal task retirement lost conversation: {conversation_id}")
    execution = await _unfinished_linear_execution_for_conversation(conversation)
    tasks = await _tasks_owned_by_execution(
        await get_tasks_for_conversation(str(conversation_id)),
        execution,
    )
    workflow_ids = {agent_task_workflow_id(task) for task in tasks if task.schedule_type == "once"}
    for task in tasks:
        task_execution = await get_work_item_execution_for_task(task.id)
        if (
            task_execution is not None
            and task_execution.status.is_active
            and task_execution.temporal_workflow_id is not None
        ):
            workflow_ids.add(task_execution.temporal_workflow_id)
    if execution is not None and execution.temporal_workflow_id is not None:
        workflow_ids.add(execution.temporal_workflow_id)
    for workflow_id in sorted(workflow_ids):
        await cancel_scheduled_agent_workflow(workflow_id)
    for task in tasks:
        await cancel_task_and_checkpoint(task.id)


async def retire_work_item_execution(execution: WorkItemExecution) -> None:
    """Stop exact durable work while preserving its conversation and session."""
    task = await get_task_by_id(execution.task_id) if execution.task_id is not None else None
    await _cancel_execution_workflows(execution, task)
    if execution.task_id is not None:
        await cancel_task_and_checkpoint(execution.task_id)
    if execution.turn_id is not None:
        await clear_in_flight_turn(execution.turn_id)


async def retire_terminal_work_item_execution_if_unowned(
    execution: WorkItemExecution,
) -> bool:
    """Atomically retire exact terminal resources with an ownership fence."""
    task = await get_task_by_id(execution.task_id) if execution.task_id is not None else None
    return await retire_terminal_execution_resources_if_unowned(
        execution,
        partial(_cancel_execution_workflows, execution, task),
    )


async def _cancel_execution_workflows(
    execution: WorkItemExecution,
    task: ScheduledTask | None,
) -> None:
    """Cancel Temporal workflows associated with one exact execution."""
    workflow_ids = (
        {execution.temporal_workflow_id} if execution.temporal_workflow_id is not None else set()
    )
    if task is not None and task.schedule_type == "once":
        workflow_ids.add(agent_task_workflow_id(task))
    for workflow_id in sorted(workflow_ids):
        await cancel_scheduled_agent_workflow(workflow_id)


async def retire_provider_work_item_execution(
    deps: TerminalConversationRetirementDeps,
    execution: WorkItemExecution,
    control_state_revision: str | None,
) -> None:
    """Apply a provider terminal snapshot through the shared conversation lifecycle."""
    conversation = await get_conversation_for_subject_key(
        ConversationSubjectKey(execution.linear_issue_id),
        workspace=GroupFolder(execution.workspace),
        namespace_suffix=":issue",
    )
    if conversation is None:
        await retire_terminal_work_item_execution_if_unowned(execution)
        return
    retirement = await retire_latest_terminal_work_item_conversation(
        conversation.id,
        execution,
        control_state_revision=control_state_revision,
    )
    # Shared conversation retirement finds bound active tasks; exact cleanup
    # also catches task and turn projections detached before binding.
    await retire_terminal_work_item_execution_if_unowned(execution)
    if retirement is None:
        return
    await retire_terminal_runtime(deps, conversation.id, retirement, set())


async def _unfinished_linear_execution_for_conversation(
    conversation: Conversation,
) -> WorkItemExecution | None:
    """Return unfinished managed work only for its matching Linear issue."""
    namespace = str(conversation.subject.namespace)
    if not namespace.startswith("linear:") or not namespace.endswith(":issue"):
        return None
    execution = await get_unfinished_work_item_execution(str(conversation.subject.key))
    if execution is None or execution.workspace != str(conversation.workspace):
        return None
    return execution


async def _tasks_owned_by_execution(
    tasks: list[ScheduledTask],
    execution: WorkItemExecution | None,
) -> list[ScheduledTask]:
    """Recover active task ownership recorded before conversation binding existed."""
    if execution is None or execution.task_id is None:
        return tasks
    if any(task.id == execution.task_id for task in tasks):
        return tasks
    task = await get_task_by_id(execution.task_id)
    if task is None or task.status not in {"active", "paused"}:
        return tasks
    return [*tasks, task]
