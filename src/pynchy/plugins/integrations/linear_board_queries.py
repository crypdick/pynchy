"""GraphQL documents used by Linear workspace board operations."""

from __future__ import annotations

CREATE_WORKSPACE_TODO_MUTATION = """
mutation CreateWorkspaceTodo(
  $team_id: String!,
  $project_id: String!,
  $state_id: String!,
  $title: String!,
  $description: String,
  $priority: Int
) {
  issueCreate(input: {
    teamId: $team_id,
    projectId: $project_id,
    stateId: $state_id,
    title: $title,
    description: $description,
    priority: $priority
  }) {
    success
    issue {
      id identifier title url
      state { id name type }
      project { id name }
    }
  }
}
"""

MOVE_WORKSPACE_TODO_MUTATION = """
mutation MoveWorkspaceTodo($issue_id: String!, $state_id: String!) {
  issueUpdate(id: $issue_id, input: {stateId: $state_id}) {
    success
    issue { id identifier title url state { id name type } }
  }
}
"""
