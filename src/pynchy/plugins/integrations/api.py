"""Curated built-in integration capabilities for composition and assurance."""

from pynchy.plugins.integrations.caldav import (
    _handle_create_event,
    _handle_delete_event,
    _handle_list_calendar,
    _handle_list_calendars,
)
from pynchy.plugins.integrations.linear import LinearClient, WorkspaceContext
from pynchy.plugins.integrations.linear_accounts import LinearAccount, linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    WorkspaceTodoProposal,
    create_workspace_todo,
    list_workspace_todos,
    move_workspace_todo,
    select_team,
)
from pynchy.plugins.integrations.linear_boot import (
    LinearIssueControl,
    create_linear_workspace_todo,
    linear_workspace_boards,
    linear_workspace_enabled,
    reconcile_linear_workspace_boards,
)
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_decision_inbox import (
    process_linear_plan_review_admission,
    reconcile_all_linear_work_items,
)
from pynchy.plugins.integrations.linear_work_items import (
    attach_work_item_pull_request,
    work_item_execution_to_dict,
)
from pynchy.plugins.integrations.matrix_route_registry import get_active_matrix_route
from pynchy.plugins.integrations.proton_bridge import ProtonMailClient, create_proton_mail_client

__all__ = [
    "LinearAccount",
    "LinearClient",
    "LinearIssueControl",
    "LinearWorkspaceBoard",
    "ProtonMailClient",
    "WorkspaceContext",
    "WorkspaceTodoProposal",
    "_handle_create_event",
    "_handle_delete_event",
    "_handle_list_calendar",
    "_handle_list_calendars",
    "attach_work_item_pull_request",
    "create_linear_workspace_todo",
    "create_proton_mail_client",
    "create_workspace_todo",
    "get_active_matrix_route",
    "linear_account_for_workspace",
    "linear_workspace_boards",
    "linear_workspace_enabled",
    "list_workspace_todos",
    "move_workspace_todo",
    "process_linear_plan_review_admission",
    "reconcile_all_linear_work_items",
    "reconcile_linear_workspace_boards",
    "resolve_linear_issue_conversation",
    "select_team",
    "work_item_execution_to_dict",
]
