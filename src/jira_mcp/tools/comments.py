"""Add comments to Jira issues."""

from typing import Annotated, Any, Literal

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from jira_mcp.app import get_client, mcp
from jira_mcp.common import CREATE, Issue, IssueId, NonemptyString, validate_payload
from jira_mcp.text import markdown_to_adf

# Jira Cloud limits comments to 32,767 characters of stored ADF, so this local cap is approximate.
CommentBody = Annotated[str, Field(min_length=1, max_length=32_767, pattern=r"\S")]


COMMENT_UNCERTAIN = "The comment may already exist; check get_issue_comments before retrying."


class Visibility(BaseModel):
    """Restrict a comment to one group or project role."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["group", "role"]
    value: Annotated[str, Field(min_length=1, max_length=255)]


class CreatedComment(BaseModel):
    """Validate the identity returned by a successful comment write."""

    id: IssueId
    created: NonemptyString


@mcp.tool(annotations=CREATE)
async def add_issue_comment(
    issue: Issue,
    body: CommentBody,
    ctx: Context,
    visibility: Visibility | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Add one comment, converting paragraphs, "- " bullet lists, and `inline code` to ADF.

    Blank lines separate paragraphs and single line breaks are kept. Other Markdown
    stays plain text. Bare URLs and issue keys pass through and Jira links them.
    Visibility limits the comment to a group or project role. validate_only returns
    the ADF that would be sent without any request. Otherwise this makes exactly one
    POST that is never retried. If it fails, times out, or is cancelled, the comment
    may already exist: check get_issue_comments before retrying.
    """
    adf = markdown_to_adf(body)
    if validate_only:
        return {"validate_only": True, "issue": issue, "adf": adf}
    payload: dict[str, Any] = {"body": adf}
    if visibility is not None:
        payload["visibility"] = visibility.model_dump()

    client = get_client(ctx)
    try:
        data = await client.post(f"issue/{issue}/comment", payload)
        created = validate_payload(CreatedComment, data)
    except ToolError as exc:
        message = f"{str(exc).rstrip('.')}. {COMMENT_UNCERTAIN}"
        raise ToolError(message) from None
    return {"id": created.id, "issue": issue, "created": created.created}
