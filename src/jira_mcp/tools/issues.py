"""Read Jira issues, bulk issue fetches, JQL search results, and comments."""

from typing import Annotated, Any
lazy from collections.abc import Sequence

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, StrictBool

from jira_mcp.app import get_client, mcp
from jira_mcp.common import (
    INVALID_PAYLOAD,
    READ_ONLY,
    Issue,
    IssueId,
    IssuePayload,
    Offset,
    PageSize,
    next_offset,
    validate_payload,
)
from jira_mcp.text import render_text

IssueSelection = Annotated[list[Issue], Field(min_length=1, max_length=20)]


TextLimit = Annotated[int, Field(ge=1, le=100_000)]


FieldId = Annotated[str, Field(pattern=r"^(?:\*all|[A-Za-z][A-Za-z0-9_]*)$", max_length=255)]


FieldSelection = Annotated[list[FieldId], Field(min_length=1, max_length=100)]


Jql = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]


PageToken = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]


DEFAULT_FIELDS = "summary,status,issuetype,priority,assignee,reporter,labels,created,updated,description"


# REST links and UI hints that agents do not use. IDs, keys, and names stay for decisions and JQL.
NOISE_KEYS = frozenset({"self", "iconUrl", "avatarUrls", "colorName"})


class BulkIssueError(BaseModel):
    """Keep only a safe Jira ID and ignore the untrusted upstream message."""

    id: IssueId


class BulkIssuePayload(BaseModel):
    """Validate the bulk envelope and require Jira's issue array."""

    issues: list[IssuePayload]
    issue_errors: list[BulkIssueError] = Field(default_factory=list, alias="issueErrors")


class SearchIssuePayload(IssuePayload):
    """Accept identity-only search projections without weakening single-issue reads."""

    fields: dict[str, Any] = Field(default_factory=dict)


class IssueSearchPage(BaseModel):
    """Validate enhanced search results and their page-level field metadata."""

    issues: list[SearchIssuePayload]
    next_page_token: PageToken | None = Field(default=None, alias="nextPageToken")
    is_last: StrictBool = Field(default=False, alias="isLast")
    names: dict[str, str] = Field(default_factory=dict)
    field_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class CommentPayload(BaseModel):
    """Preserve comment identity and content without avatar or REST link noise."""

    id: str
    author: dict[str, Any] | None = None
    created: str | None = None
    updated: str | None = None
    body: Any = None
    visibility: dict[str, Any] | None = None
    parent_id: str | None = Field(default=None, alias="parentId")
    jsd_public: bool | None = Field(default=None, alias="jsdPublic")


class CommentPage(BaseModel):
    """Validate Jira's offset-based comment envelope."""

    startAt: int = Field(ge=0)  # noqa: N815 - Jira's wire format.
    total: int = Field(ge=0)
    comments: list[CommentPayload]


@mcp.tool(annotations=READ_ONLY)
async def get_issue(  # noqa: PLR0913 - Explicit arguments keep MCP calls self-contained.
    issue: Issue,
    ctx: Context,
    *,
    fields: FieldSelection | None = None,
    include_raw_adf: bool = False,
    text_limit: TextLimit = 12_000,
    include_raw_fields: bool = False,
    include_field_metadata: bool = False,
) -> dict[str, Any]:
    """Get one ticket by key or numeric ID, including its description by default.

    Explicit fields replace defaults. Request ['*all'] only when the full potentially
    large field set is needed. list_fields can discover IDs before requesting values.
    Field values are Jira JSON without self, iconUrl, avatarUrls, or colorName keys
    at any depth; set include_raw_fields to keep them. Description is returned
    separately as text with character count and truncation status. Request raw ADF
    to retain rich content. Set include_field_metadata to add field_metadata, which
    maps returned field IDs (except description) to their names and schemas.
    """
    params = {"fields": ",".join(fields) if fields is not None else DEFAULT_FIELDS}
    if include_field_metadata:
        params["expand"] = "names,schema"
    payload = validate_payload(IssuePayload, await get_client(ctx).get(f"issue/{issue}", params=params))
    result = render_issue(
        payload, text_limit=text_limit, include_raw_adf=include_raw_adf, include_raw_fields=include_raw_fields
    )
    if include_field_metadata:
        result["field_metadata"] = field_metadata([payload], payload.names, payload.field_schema)
    return result


@mcp.tool(annotations=READ_ONLY)
async def get_issues(  # noqa: PLR0913 - Explicit arguments keep MCP calls self-contained.
    issues: IssueSelection,
    ctx: Context,
    *,
    fields: FieldSelection | None = None,
    include_raw_adf: bool = False,
    text_limit: TextLimit = 12_000,
    include_raw_fields: bool = False,
    include_field_metadata: bool = False,
) -> dict[str, Any]:
    """Fetch 1 to 20 Jira tickets by key or numeric ID in one read-only request.

    Exact duplicate input strings are collapsed after the 20-item bound is checked.
    Fields replace the same defaults as get_issue; ['*all'] requests all fields.
    Results use get_issue's field and description format. The text limit applies
    per issue, and optional raw ADF is not truncated. Optional field_metadata is
    returned once for all issues, not per issue.

    Jira returns canonical IDs and keys in its own order, normally ascending ID,
    not input order. Missing or inaccessible tickets can be silently omitted from
    both issues and errors. Errors contain only an ID and a fixed safe message.
    omissions_possible is always true and is not a request to retry. This tool
    makes no automatic follow-up requests.
    """
    body: dict[str, Any] = {
        "issueIdsOrKeys": list(dict.fromkeys(issues)),
        "fields": list(fields) if fields is not None else DEFAULT_FIELDS.split(","),
    }
    if include_field_metadata:
        body["expand"] = ["names", "schema"]
    payload = validate_payload(BulkIssuePayload, await get_client(ctx).post("issue/bulkfetch", body))
    result: dict[str, Any] = {}
    if include_field_metadata:
        # Bulk fetch returns metadata per issue, but field IDs are site-wide, so one merged map is enough.
        names = {key: value for issue in payload.issues for key, value in issue.names.items()}
        schemas = {key: value for issue in payload.issues for key, value in issue.field_schema.items()}
        result["field_metadata"] = field_metadata(payload.issues, names, schemas)
    return result | {
        "issues": [
            render_issue(
                issue,
                text_limit=text_limit,
                include_raw_adf=include_raw_adf,
                include_raw_fields=include_raw_fields,
            )
            for issue in payload.issues
        ],
        "errors": [{"id": error.id, "message": "Jira could not return this issue."} for error in payload.issue_errors],
        "omissions_possible": True,
    }


def strip_noise(value: Any) -> Any:
    """Drop REST link and UI keys at any depth while keeping nulls and all other values."""
    if isinstance(value, dict):
        return {key: strip_noise(item) for key, item in value.items() if key not in NOISE_KEYS}
    if isinstance(value, list):
        return [strip_noise(item) for item in value]
    return value


def field_metadata(
    issues: Sequence[IssuePayload], names: dict[str, str], schemas: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Map returned field IDs to known names and schemas, once for all issues.

    Description is left out because it is rendered as its own top-level text envelope.
    """
    field_ids = dict.fromkeys(field_id for issue in issues for field_id in issue.fields)
    return {
        field_id: {
            **({"name": names[field_id]} if field_id in names else {}),
            **({"schema": schemas[field_id]} if field_id in schemas else {}),
        }
        for field_id in field_ids
        if field_id != "description" and (field_id in names or field_id in schemas)
    }


def render_issue(
    payload: IssuePayload, *, text_limit: int, include_raw_adf: bool, include_raw_fields: bool
) -> dict[str, Any]:
    """Render selected fields without losing description status, nulls, or field IDs."""
    values = payload.fields.copy()
    result = {"id": payload.id, "key": payload.key, "fields": values}
    if "description" in values:
        result["description"] = render_text(values.pop("description"), limit=text_limit, include_raw=include_raw_adf)
    if not include_raw_fields:
        result["fields"] = {field_id: strip_noise(value) for field_id, value in values.items()}
    return result


@mcp.tool(annotations=READ_ONLY)
async def search_issues(  # noqa: PLR0913 - Explicit arguments keep MCP calls self-contained.
    jql: Jql,
    ctx: Context,
    *,
    next_page_token: PageToken | None = None,
    max_results: PageSize = 20,
    fields: FieldSelection | None = None,
    include_raw_adf: bool = False,
    text_limit: TextLimit = 2_000,
    include_raw_fields: bool = False,
    include_field_metadata: bool = False,
) -> dict[str, Any]:
    """Search ticket titles, descriptions, and metadata using bounded Jira JQL.

    Use summary ~ 'term' for titles, description ~ 'term' for descriptions, or
    text ~ 'term' for broader text including comments. Use JQL comparisons for
    project, status, labels, people, and searchable custom fields. For example:
    project = DEMO AND (summary ~ 'gate' OR labels = patch-mode).
    Jira's indexed text syntax is not natural-language or substring matching.
    Restrict queries with a project or another bounded clause.

    Return one page with no inferred total. Pass next_page_token unchanged with
    the same JQL and options until null, even after short or empty pages. Search
    only includes visible tickets and recent changes can lag behind the index.
    Fields replace defaults, but key is always requested for identity. Responses
    use get_issue's field and description format, including the raw field
    opt-in; text_limit applies per issue. Optional field_metadata is returned
    once per page, not per issue.
    The enhanced search POST only reads data and avoids URL-length limits.
    """
    selected = list(fields) if fields is not None else DEFAULT_FIELDS.split(",")
    if "key" not in selected:
        selected.append("key")
    body: dict[str, Any] = {"jql": jql, "maxResults": max_results, "fields": selected}
    if include_field_metadata:
        body["expand"] = "names,schema"
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    page = validate_payload(IssueSearchPage, await get_client(ctx).post("search/jql", body))
    # Jira may omit isLast, but an explicit flag must agree with its continuation token.
    if "is_last" in page.model_fields_set and page.is_last != (page.next_page_token is None):
        raise ToolError(INVALID_PAYLOAD)
    result: dict[str, Any] = {}
    if include_field_metadata:
        result["field_metadata"] = field_metadata(page.issues, page.names, page.field_schema)
    return result | {
        "issues": [
            render_issue(
                issue,
                text_limit=text_limit,
                include_raw_adf=include_raw_adf,
                include_raw_fields=include_raw_fields,
            )
            for issue in page.issues
        ],
        "next_page_token": page.next_page_token,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_issue_comments(  # noqa: PLR0913 - Explicit arguments keep MCP calls self-contained.
    issue: Issue,
    ctx: Context,
    *,
    start_at: Offset = 0,
    max_results: PageSize = 20,
    include_raw_adf: bool = False,
    text_limit: TextLimit = 4_000,
) -> dict[str, Any]:
    """Get one oldest-first page with author, dates, visibility, reply IDs, and body.

    Pass next_start_at back as start_at until null. Jira may clamp page sizes or
    hide restricted comments. Text limits apply to each body, not the full page.
    Raw ADF is optional and is not truncated when requested.
    Preserve parentId and jsdPublic when Jira supplies reply or public/internal status.
    """
    data = await get_client(ctx).get(
        f"issue/{issue}/comment",
        params={"startAt": start_at, "maxResults": max_results, "orderBy": "created"},
    )
    page = validate_payload(CommentPage, data)
    comments = []
    for comment in page.comments:
        item = comment.model_dump(by_alias=True, exclude_none=True)
        if comment.author is not None:
            item["author"] = {key: comment.author[key] for key in ("accountId", "displayName") if key in comment.author}
        item["body"] = render_text(comment.body, limit=text_limit, include_raw=include_raw_adf)
        comments.append(item)
    return {
        "comments": comments,
        "start_at": page.startAt,
        "total": page.total,
        "next_start_at": next_offset(page.startAt, len(comments), page.total),
    }
