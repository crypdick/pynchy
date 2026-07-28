"""Durable task retirement for terminal Linear conversations."""

from __future__ import annotations

from pynchy.conversation.api import (  # noqa: TC001, RUF100 - beartype resolves adapter annotations at runtime.
    Conversation,
    ConversationId,
)
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.temporal.workflow_control import cancel_scheduled_agent_workflow
from pynchy.scheduling.api import (
    ScheduledTask,  # noqa: TC001, RUF100 - beartype resolves adapter annotations at runtime.
)
from pynchy.state.api import (
    cancel_task_and_checkpoint,
    get_conversation,
    get_task_by_id,
    get_tasks_for_conversation,
    get_unfinished_work_item_execution,
    get_work_item_execution_for_task,
)
from pynchy.work_items.api import (
    WorkItemExecution,  # noqa: TC001, RUF100 - beartype resolves adapter annotations at runtime.
)


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
