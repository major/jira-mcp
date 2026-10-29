"""Discover and apply Jira workflow transitions."""

from typing import Any

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from jira_mcp.app import get_client, mcp
from jira_mcp.common import READ_ONLY, WRITE, WRITE_OUTCOME_UNKNOWN, Issue, IssueId, NonemptyString, validate_payload


class TransitionPayload(BaseModel):
    """Keep transition metadata and validate its identity, status, and fields."""

    model_config = ConfigDict(extra="allow")

    id: IssueId
    name: NonemptyString
    to: dict[str, Any]
    fields: dict[str, dict[str, Any]]


class TransitionPage(BaseModel):
    """Validate Jira's complete transition discovery envelope."""

    transitions: list[TransitionPayload]


@mcp.tool(annotations=READ_ONLY)
async def get_issue_transitions(issue: Issue, ctx: Context) -> dict[str, Any]:
    """Discover transitions and their fields, allowed values, and target statuses.

    Field IDs and nested metadata remain unchanged, including resolution options.
    Available transitions and required fields can change with permissions or state.
    """
    data = await get_client(ctx).get(f"issue/{issue}/transitions", params={"expand": "transitions.fields"})
    page = validate_payload(TransitionPage, data)
    return {"transitions": [transition.model_dump() for transition in page.transitions]}


@mcp.tool(annotations=WRITE)
async def transition_issue(
    issue: Issue, transition_id: IssueId, ctx: Context, fields: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Apply a transition, optionally supplying field IDs and original JSON values.

    Discover valid transitions and required fields first. Success acknowledges Jira's
    response and does not infer the final status. Writes are never retried. If an
    error leaves the outcome unknown, inspect the issue before attempting again.
    """
    body: dict[str, Any] = {"transition": {"id": transition_id}}
    if fields is not None:
        body["fields"] = fields
    client = get_client(ctx)
    try:
        await client.post(f"issue/{issue}/transitions", body, expect_json=False)
    except ToolError as exc:
        message = f"{str(exc).rstrip('.')}. {WRITE_OUTCOME_UNKNOWN}"
        raise ToolError(message) from None
    return {"issue": issue, "transition_id": transition_id, "success": True}
