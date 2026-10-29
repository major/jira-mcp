"""Discover Jira field IDs, names, and schemas globally or on one issue."""

from typing import Any

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.

from jira_mcp.app import get_client, get_field_catalog, mcp
from jira_mcp.common import (
    READ_ONLY,
    Issue,
    IssuePayload,
    Offset,
    PageSize,
    next_offset,
    validate_payload,
)
from jira_mcp.fields import FieldDefinition


class IssueFieldDefinition(FieldDefinition):
    """Describe an issue field without exposing its original value."""

    has_value: bool


@mcp.tool(annotations=READ_ONLY)
async def list_fields(  # noqa: PLR0913 - Explicit arguments keep MCP calls self-contained.
    ctx: Context,
    search: str | None = None,
    start_at: Offset = 0,
    max_results: PageSize = 50,
    *,
    issue: Issue | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Discover field IDs, names, and schemas globally or on a specific issue.

    Search matches IDs or names case-insensitively. Results are sorted by ID and
    paged locally. Duplicate names are possible: use IDs in get_issue(fields=[...]).
    The global catalog is cached for five minutes; refresh=true forces a reload.
    Issue discovery is fresh, ignores refresh, and reports has_value for non-null
    fields without returning values. Missing fields are not the same as null fields.
    """
    if issue is None:
        fields = await get_field_catalog(ctx).get(refresh=refresh)
    else:
        data = await get_client(ctx).get(f"issue/{issue}", params={"fields": "*all", "expand": "names,schema"})
        payload = validate_payload(IssuePayload, data)
        fields = [
            validate_payload(
                IssueFieldDefinition,
                {
                    "id": field_id,
                    "name": payload.names.get(field_id, field_id),
                    "custom": field_id.startswith("customfield_"),
                    "schema": payload.field_schema.get(field_id),
                    "has_value": value is not None,
                },
            )
            for field_id, value in payload.fields.items()
        ]
    query = (search or "").casefold()
    matches = sorted(
        (field for field in fields if query in field.id.casefold() or query in field.name.casefold()),
        key=lambda field: field.id,
    )
    page = matches[start_at : start_at + max_results]
    return {
        "fields": [field.model_dump(by_alias=True, exclude_none=True) for field in page],
        "start_at": start_at,
        "total": len(matches),
        "next_start_at": next_offset(start_at, len(page), len(matches)),
    }
