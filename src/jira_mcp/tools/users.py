"""Find the authenticated Jira account and other users by name or email."""

from typing import Annotated, Any

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from pydantic import BaseModel, Field, RootModel

from jira_mcp.app import get_client, mcp
from jira_mcp.common import READ_ONLY, PageSize, next_offset, validate_payload

USER_SEARCH_LIMIT = 1000


UserOffset = Annotated[int, Field(ge=0, le=USER_SEARCH_LIMIT - 1)]


UserQuery = Annotated[str, Field(min_length=1, max_length=255, pattern=r"\S")]


class UserPayload(BaseModel):
    """Keep stable account identity and visible profile details without REST noise."""

    account_id: str = Field(alias="accountId", min_length=1)
    display_name: str = Field(alias="displayName", min_length=1)
    email_address: str | None = Field(default=None, alias="emailAddress")
    active: bool | None = None
    account_type: str | None = Field(default=None, alias="accountType")


class UserSearchPayload(RootModel[list[UserPayload]]):
    """Validate the user search array, which has no total or pagination envelope."""


@mcp.tool(annotations=READ_ONLY)
async def get_current_user(ctx: Context) -> dict[str, Any]:
    """Identify the Jira account authenticated by the server's configured credentials.

    Return accountId, displayName, and visible emailAddress, active, and accountType
    fields. Email may be hidden by privacy settings. This is the credential owner,
    not necessarily the person chatting with the LLM.
    """
    data = await get_client(ctx).get("myself")
    return validate_payload(UserPayload, data).model_dump(by_alias=True, exclude_none=True)


@mcp.tool(annotations=READ_ONLY)
async def search_users(
    query: UserQuery,
    ctx: Context,
    start_at: UserOffset = 0,
    max_results: PageSize = 20,
) -> dict[str, Any]:
    """Find active Jira users by name or email and return stable accountId values.

    Names can be duplicated: disambiguate matches rather than choosing the first.
    Email may be hidden by privacy settings. Requires Browse users and groups;
    missing permission can produce an empty list, not proof that no user exists.
    Results do not establish whether a user can be assigned a particular issue.
    Pass next_start_at as start_at until null, even after a short page. Jira exposes
    only the first 1,000 matches; limit_reached means narrow the query to find more.
    """
    data = await get_client(ctx).get(
        "user/search",
        params={"query": query, "startAt": start_at, "maxResults": min(max_results, USER_SEARCH_LIMIT - start_at)},
    )
    users = validate_payload(UserSearchPayload, data).root
    end = start_at + len(users)
    return {
        "users": [user.model_dump(by_alias=True, exclude_none=True) for user in users],
        "start_at": start_at,
        # Continue short pages because Jira may clamp the requested page size.
        "next_start_at": next_offset(start_at, len(users), USER_SEARCH_LIMIT),
        "limit_reached": end >= USER_SEARCH_LIMIT,
    }
