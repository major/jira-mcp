"""Add and edit comments on Jira issues."""

import copy
import difflib
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from jira_mcp.app import get_client, mcp
from jira_mcp.client import JiraHTTPError
from jira_mcp.common import (
    ADF_MAX_BYTES,
    CREATE,
    WRITE,
    Issue,
    IssueId,
    NonemptyString,
    validate_description_adf,
    validate_payload,
)
from jira_mcp.text import hash_adf, markdown_to_adf, render_text

# Jira Cloud limits comments to 32,767 characters of stored ADF, so this local cap is approximate.
CommentBody = Annotated[str, Field(min_length=1, max_length=32_767, pattern=r"\S")]


COMMENT_UNCERTAIN = "The comment may already exist; check get_issue_comments before retrying."
DIFF_LIMIT = 4000
CommentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OldText = Annotated[str, Field(min_length=1, max_length=32_767)]
NewText = Annotated[str, Field(max_length=32_767)]


def _validate_comment_adf(adf: Any) -> dict[str, Any]:
    """Validate the comment envelope and its compact UTF-8 size."""
    if not isinstance(adf, dict):
        msg = "Invalid comment ADF: expected an ADF document envelope."
        raise ToolError(msg)
    try:
        return validate_description_adf(adf)
    except ToolError as exc:
        raise ToolError(str(exc).replace("description_adf", "comment ADF")) from None


def _text_nodes(node: dict[str, Any]) -> list[tuple[list[Any], dict[str, Any]]]:
    """Find text nodes only through ADF content arrays, retaining their parent lists."""
    found = []
    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            if isinstance(child, dict):
                if child.get("type") == "text" and isinstance(child.get("text"), str):
                    found.append((content, child))
                else:
                    found.extend(_text_nodes(child))
    return found


def _replace_text(adf: dict[str, Any], old: str, new: str, *, replace_all: bool) -> None:
    """Replace exact nonoverlapping matches without changing unrelated ADF fields."""
    nodes = _text_nodes(adf)
    count = sum(node["text"].count(old) for _, node in nodes)
    if not count:
        msg = "Text not found in a single text node."
        raise ToolError(msg)
    if count > 1 and not replace_all:
        msg = "Text occurs more than once; use replace_all to replace every match."
        raise ToolError(msg)
    # Bound allocation before str.replace; serialized ADF gets its exact size check afterward.
    projected_bytes = sum(len(node["text"].encode("utf-8")) for _, node in nodes)
    projected_bytes += count * (len(new.encode("utf-8")) - len(old.encode("utf-8")))
    if projected_bytes > ADF_MAX_BYTES:
        msg = "Replacement would exceed the 100,000-byte comment ADF limit."
        raise ToolError(msg)
    for parent, node in nodes:
        if old in node["text"]:
            node["text"] = node["text"].replace(old, new)
            if not node["text"]:
                # Remove by identity, since distinct nodes can have equal values.
                parent[:] = [child for child in parent if child is not node]


def _comment_diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, bool]:
    """Create a bounded, lossy rendered-text diff for preview."""
    left = render_text(before, limit=DIFF_LIMIT, include_raw=False)
    right = render_text(after, limit=DIFF_LIMIT, include_raw=False)
    diff = "".join(
        difflib.unified_diff(
            [line + "\n" for line in left["text"].splitlines()],
            [line + "\n" for line in right["text"].splitlines()],
        )
    )
    return diff[:DIFF_LIMIT], left["truncated"] or right["truncated"] or len(diff) > DIFF_LIMIT


@mcp.tool(annotations=WRITE)
async def edit_issue_comment(  # noqa: C901, PLR0912, PLR0913
    issue: Issue,
    comment_id: IssueId,
    ctx: Context,
    *,
    expected_hash: CommentHash | None = None,
    body: CommentBody | None = None,
    body_adf: dict[str, Any] | None = None,
    old_text: OldText | None = None,
    new_text: NewText | None = None,
    replace_all: bool = False,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Edit a comment using a whole Markdown or raw ADF body, or exact text replacement.

    body_adf is a full replacement, not a partial merge. Fetch the complete body.adf
    and body_hash with get_issue_comments(include_raw_adf=true, include_hashes=true)
    and retain unrelated nodes. Raw ADF validation checks only the document envelope
    and 100,000 compact UTF-8 JSON bytes, not nested Jira validation.
    Text replacements preserve newlines literally and cannot span nodes. Preview performs
    a GET and returns ADF, hashes and a bounded, lossy text diff, not a complete ADF diff.
    Use the preview before_hash, not after_hash, as the optional guard for execution.
    The optional hash checks the complete original ADF, but GET and PUT are not atomic
    and visibility changes are not guarded. Execution sends one PUT with only the body;
    its returned hash describes submitted ADF, not necessarily persisted content.
    On an uncertain or cancelled write, inspect get_issue_comments before retrying.
    """
    if body_adf is not None:
        if body is not None or old_text is not None or new_text is not None or replace_all:
            msg = "body_adf and other replacement modes are mutually exclusive."
            raise ToolError(msg)
        _validate_comment_adf(body_adf)
    elif body is not None:
        if old_text is not None or new_text is not None or replace_all:
            msg = "body and text replacement modes are mutually exclusive."
            raise ToolError(msg)
    elif old_text is None or new_text is None:
        msg = "Provide body, body_adf, or both old_text and new_text."
        raise ToolError(msg)

    client = get_client(ctx)
    path = f"issue/{issue}/comment/{comment_id}"
    data = await client.get(path)
    original = _validate_comment_adf(data.get("body") if isinstance(data, dict) else None)
    before_hash = hash_adf(original)
    if expected_hash is not None and expected_hash != before_hash:
        msg = "Comment hash precondition did not match; no edit was made."
        raise ToolError(msg)
    if body is not None:
        edited = markdown_to_adf(body)
    elif body_adf is not None:
        edited = body_adf
    elif old_text is not None and new_text is not None:
        edited = copy.deepcopy(original)
        _replace_text(edited, old_text, new_text, replace_all=replace_all)
    else:
        msg = "Provide body, body_adf, or both old_text and new_text."
        raise ToolError(msg)
    _validate_comment_adf(edited)
    after_hash = hash_adf(edited)
    if validate_only:
        diff, truncated = _comment_diff(original, edited)
        return {
            "issue": issue,
            "comment_id": comment_id,
            "validate_only": True,
            "adf": edited,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "diff": diff,
            "diff_truncated": truncated,
        }
    try:
        await client.put(path, {"body": edited})
    except ToolError as exc:
        if isinstance(exc, JiraHTTPError) and exc.status_code in {400, 401, 403, 404, 422, 429}:
            hint = (
                " Check Edit own comments or Edit all comments permissions."
                if exc.status_code == HTTPStatus.FORBIDDEN
                else ""
            )
            msg = f"Jira rejected the edit with HTTP status {exc.status_code}; no edit was made.{hint}"
            raise ToolError(msg) from None
        msg = "The edit outcome is unknown; inspect get_issue_comments before retrying."
        raise ToolError(msg) from None
    return {"issue": issue, "comment_id": comment_id, "updated": True, "body_hash": after_hash}


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
    """Add one comment, converting a small Markdown subset to ADF.

    Blank lines separate paragraphs and single line breaks are kept. The converter
    supports headings, flat bullet and ordered lists, inline code, bold and italic, and explicit
    http, https, or mailto links. Other Markdown stays plain text. Bare URLs and
    issue keys pass through for Jira to link.
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
