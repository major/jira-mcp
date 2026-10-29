"""Assign or unassign Jira issues."""

from typing import Any

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import MISSING, BaseModel, Field

from jira_mcp.app import get_client, mcp
from jira_mcp.client import JiraHTTPError
from jira_mcp.common import WRITE, WRITE_OUTCOME_UNKNOWN, AccountId, Issue, NonemptyString, validate_payload

NOT_CHANGED = "No change was made"


# Only statuses where Jira refuses before changing anything; any other failure is uncertain.
ASSIGN_REJECTED = {
    400: "the user may not be assignable, or the project may not allow unassigned issues",
    401: "check the credentials and token scopes",
    403: "check the Assign issues permission",
    404: "check the issue and the account ID",
}


class Assignee(BaseModel):
    """Keep only the assignee's account ID."""

    account_id: NonemptyString = Field(alias="accountId")


class AssigneeFields(BaseModel):
    """Require the assignee field so a hidden field is never mistaken for unassigned."""

    assignee: Assignee | None


class AssigneeIssue(BaseModel):
    """Validate an issue read limited to the assignee field."""

    key: NonemptyString
    fields: AssigneeFields


@mcp.tool(annotations=WRITE)
async def assign_issue(
    issue: Issue,
    account_id: AccountId | None,
    ctx: Context,
    # MISSING tells an omitted guard apart from an explicit null, which means "must be unassigned".
    expected_assignee: AccountId | MISSING | None = MISSING,
) -> dict[str, Any]:
    """Assign an issue to an account ID, or unassign it with account_id=null.

    Find account IDs with search_users or get_current_user. Jira treats "-1" as the
    project default assignee, and the result then reports "-1". The tool first reads
    the current assignee. If expected_assignee is given, it changes nothing unless the
    assignee matches at read time (null means the issue must be unassigned), so taking
    an unassigned ticket needs one call. Then it makes exactly one PUT that is never
    retried. Only errors that say the outcome may be unknown need an issue check
    before retrying; all other errors happen before any change.
    """
    client = get_client(ctx)
    current = validate_payload(AssigneeIssue, await client.get(f"issue/{issue}", params={"fields": "assignee"}))
    previous = None if current.fields.assignee is None else current.fields.assignee.account_id
    if expected_assignee is not MISSING and previous != expected_assignee:
        msg = (
            f"{NOT_CHANGED}: {current.key} is assigned to {previous or 'nobody'}, not {expected_assignee or 'nobody'}."
        )
        raise ToolError(msg)

    try:
        await client.put(f"issue/{issue}/assignee", {"accountId": account_id})
    except ToolError as exc:
        if isinstance(exc, JiraHTTPError) and (hint := ASSIGN_REJECTED.get(exc.status_code)):
            msg = f"{NOT_CHANGED}: Jira rejected the assignment with HTTP status {exc.status_code}; {hint}."
            raise ToolError(msg) from None
        message = f"{exc}. {WRITE_OUTCOME_UNKNOWN}"
        raise ToolError(message) from None
    return {"issue": current.key, "assignee": account_id, "previous_assignee": previous}
