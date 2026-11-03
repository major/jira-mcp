"""Find and create private Jira saved filters."""

# ruff: noqa: TRY003, TRY301, C901, PLR0912

from typing import Annotated, Any

from fastmcp import Context  # noqa: TC002
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from jira_mcp.app import get_client, mcp
from jira_mcp.client import JiraClient, JiraHTTPError
from jira_mcp.common import (
    CREATE,
    READ_ONLY,
    AccountId,
    Description,
    IssueId,
    Offset,
    PageSize,
    Summary,
    validate_payload,
)

Jql = Annotated[str, Field(min_length=1, max_length=100_000, pattern=r"\S")]
UNCERTAIN = "The filter may have been created; inspect search_filters before retrying. Do not retry automatically."


class Filter(BaseModel):
    """Keep only useful filter fields."""

    id: IssueId
    name: Summary
    jql: str | None = None
    view_url: str | None = Field(default=None, alias="viewUrl")


class FilterIdentity(BaseModel):
    """Validate an ID independently of the rest of a creation response."""

    id: IssueId


class FilterPage(BaseModel):
    """Validate Jira's filter search page."""

    start_at: Offset = Field(alias="startAt")
    max_results: PageSize = Field(alias="maxResults")
    total: Offset | None = None
    is_last: bool | None = Field(default=None, alias="isLast")
    values: list[Filter]

    def next_start(self) -> int | None:
        """Use the upstream window rather than the number of returned rows."""
        if self.is_last is True:
            return None
        end = self.start_at + self.max_results
        if self.is_last is False:
            return end
        if self.total is None:
            raise ToolError("Jira returned incomplete filter pagination.")
        return end if end < self.total else None


class Account(BaseModel):
    """Validate the current account identity."""

    account_id: AccountId = Field(alias="accountId")


class ParseQuery(BaseModel):
    """Validate one strict parser result."""

    query: str
    structure: dict[str, Any] | None = None
    errors: list[Any] = Field(default_factory=list)


class ParseResult(BaseModel):
    """Validate the parser response envelope."""

    queries: list[ParseQuery]


class SearchIssue(BaseModel):
    """Validate the issue key returned by the JQL probe."""

    key: str = Field(min_length=1)


class SearchResult(BaseModel):
    """Validate the JQL probe response."""

    issues: list[SearchIssue]


async def _account(client: JiraClient) -> str:
    """Get the account ID without returning profile details."""
    return validate_payload(Account, await client.get("myself")).account_id


async def _page(client: JiraClient, params: dict[str, Any]) -> FilterPage:
    """Fetch exactly one validated filter page."""
    return validate_payload(FilterPage, await client.get("filter/search", params=params))


def _params(start_at: int, max_results: int) -> dict[str, Any]:
    """Build common filter search parameters."""
    return {"expand": "jql,viewUrl", "startAt": start_at, "maxResults": max_results}


def _compact(page: FilterPage) -> dict[str, Any]:
    """Return compact rows and upstream pagination metadata."""
    result: dict[str, Any] = {
        "filters": [item.model_dump(by_alias=True, exclude_none=True) for item in page.values],
        "start_at": page.start_at,
        "max_results": page.max_results,
        "next_start_at": page.next_start(),
    }
    if page.total is not None:
        result["total"] = page.total
    return result


@mcp.tool(annotations=READ_ONLY)
async def search_filters(
    name: Summary, ctx: Context, start_at: Offset = 0, max_results: PageSize = 20
) -> dict[str, Any]:
    """Search visible filters by name, including filters owned by others, one page at a time."""
    return _compact(await _page(get_client(ctx), {**_params(start_at, max_results), "filterName": name}))


@mcp.tool(annotations=READ_ONLY)
async def list_my_filters(ctx: Context, start_at: Offset = 0, max_results: PageSize = 20) -> dict[str, Any]:
    """List one page of filters owned by the authenticated account."""
    client = get_client(ctx)
    return _compact(await _page(client, {**_params(start_at, max_results), "accountId": await _account(client)}))


@mcp.tool(annotations=CREATE)
async def create_filter(
    name: Summary, jql: Jql, ctx: Context, description: Description | None = None, validate_only: bool = False
) -> dict[str, Any]:
    """Validate JQL and create one private filter, or preview without creating.

    Validation uses read-only POSTs. The default scope and duplicate checks are best effort,
    not atomic. Missing editPermissions in Jira's response does not guarantee edit privacy.
    If creation fails or is cancelled, inspect search_filters before retrying; never retry automatically.
    """
    client = get_client(ctx)
    payload: dict[str, Any] = {"name": name, "jql": jql, "sharePermissions": [], "editPermissions": []}
    if description is not None:
        payload["description"] = description
    try:
        parsed = validate_payload(ParseResult, await client.post("jql/parse?validation=strict", {"queries": [jql]}))
        if (
            len(parsed.queries) != 1
            or parsed.queries[0].query != jql
            or parsed.queries[0].errors
            or parsed.queries[0].structure is None
        ):
            raise ToolError("Strict JQL validation failed.")
        validate_payload(
            SearchResult, await client.post("search/jql", {"jql": jql, "maxResults": 1, "fields": ["key"]})
        )
        if validate_only:
            return {"validate_only": True, "payload": payload}
        scope = await client.get("filter/defaultShareScope")
        if not isinstance(scope, dict) or scope.get("scope") != "PRIVATE":
            raise ToolError("The default filter sharing scope is not PRIVATE.")
        account = await _account(client)
        page = await _page(client, {**_params(0, 100), "filterName": name, "accountId": account})
        if any(item.name == name for item in page.values):
            raise ToolError("A filter with this name already exists.")
        if page.start_at != 0 or page.next_start() is not None:
            raise ToolError("The duplicate check is incomplete.")
    except ToolError as exc:
        raise ToolError(f"No filter was created. {exc}") from None

    try:
        data = await client.post("filter", payload)
    except JiraHTTPError as exc:
        if exc.status_code in (400, 401, 403, 404):
            raise ToolError("No filter was created. Check the name, JQL, permissions, and duplicates.") from None
        raise ToolError(UNCERTAIN) from None
    except ToolError:
        raise ToolError(UNCERTAIN) from None

    # Preserve a validated ID even when the rest of a successful response is unsafe.
    try:
        identity = validate_payload(FilterIdentity, data)
    except ToolError:
        raise ToolError(UNCERTAIN) from None
    if (
        not isinstance(data, dict)
        or data.get("sharePermissions") != []
        or ("editPermissions" in data and data["editPermissions"] != [])
    ):
        raise ToolError(f"Filter {identity.id} was created, but privacy was not confirmed. {UNCERTAIN}")
    try:
        created = validate_payload(Filter, data)
    except ToolError:
        msg = f"Filter {identity.id} was created, but its response was incomplete. {UNCERTAIN}"
        raise ToolError(msg) from None
    return {**created.model_dump(by_alias=True, exclude_none=True), "sharePermissions": []}
