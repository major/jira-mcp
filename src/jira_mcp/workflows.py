"""Expose focused Jira transitions and issue-link operations."""

from typing import Annotated, Any
from unicodedata import category
from urllib.parse import urlsplit

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, RootModel

from jira_mcp.app import get_client, mcp
from jira_mcp.common import READ_ONLY, WRITE, Issue, IssueId, id_or_key, validate_payload

WRITE_OUTCOME_UNKNOWN = "The write outcome may be unknown; inspect the issue or links before retrying."
NonemptyString = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RemoteUrl = Annotated[str, Field(min_length=1, max_length=4096)]
RemoteTitle = Annotated[str, Field(min_length=1, max_length=255, pattern=r"\S")]


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


class LinkTypePayload(BaseModel):
    """Keep link identity and both human-readable directions."""

    id: IssueId
    name: NonemptyString
    inward: NonemptyString
    outward: NonemptyString


class LinkTypePage(BaseModel):
    """Validate the issue-link type discovery envelope."""

    issue_link_types: list[LinkTypePayload] = Field(alias="issueLinkTypes")


class RemoteObjectPayload(BaseModel):
    """Keep external object metadata while requiring a URL and title."""

    model_config = ConfigDict(extra="allow")

    url: NonemptyString
    title: str
    summary: str | None = None
    icon: dict[str, Any] | None = None
    status: dict[str, Any] | None = None


class RemoteLinkPayload(BaseModel):
    """Validate each remote link and keep its non-REST metadata."""

    model_config = ConfigDict(extra="allow")

    id: PositiveInt | NonemptyString
    object: RemoteObjectPayload
    global_id: str | None = Field(default=None, alias="globalId")
    application: dict[str, Any] | None = None
    relationship: str | None = None
    self_url: Any = Field(default=None, alias="self", exclude=True)


class RemoteLinkList(RootModel[list[RemoteLinkPayload]]):
    """Validate every remote-link entry before using any of them."""


class CreatedRemoteLink(BaseModel):
    """Validate the actual Jira creation response before reporting its ID."""

    id: PositiveInt
    self_url: NonemptyString = Field(alias="self")


@mcp.tool(annotations=READ_ONLY)
async def get_issue_transitions(issue: Issue, ctx: Context) -> dict[str, Any]:
    """Discover transitions and their fields, allowed values, and target statuses.

    Field IDs and nested metadata remain unchanged, including resolution options.
    Available transitions and required fields can change with permissions or state.
    """
    data = await get_client(ctx).get(f"issue/{issue}/transitions", params={"expand": "transitions.fields"})
    page = validate_payload(TransitionPage, data)
    return {"transitions": [transition.model_dump() for transition in page.transitions]}


@mcp.tool(annotations=READ_ONLY)
async def list_issue_link_types(ctx: Context) -> dict[str, Any]:
    """List issue-link type IDs with their inward and outward direction names."""
    page = validate_payload(LinkTypePage, await get_client(ctx).get("issueLinkType"))
    return {"issue_link_types": [link_type.model_dump() for link_type in page.issue_link_types]}


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
        message = f"{exc} {WRITE_OUTCOME_UNKNOWN}"
        raise ToolError(message) from None
    return {"issue": issue, "transition_id": transition_id, "success": True}


@mcp.tool(annotations=WRITE)
async def create_issue_link(
    link_type_id: IssueId, inward_issue: Issue, outward_issue: Issue, ctx: Context
) -> dict[str, Any]:
    """Link two issues using an explicit type ID and inward/outward direction.

    For the standard Blocks type, inward_issue blocks outward_issue. Discover
    type directions first. Success acknowledges Jira's response, not a newly
    created ID. Writes are never retried; inspect links after an ambiguous error
    before trying again.
    """
    body = {
        "type": {"id": link_type_id},
        "inwardIssue": id_or_key(inward_issue),
        "outwardIssue": id_or_key(outward_issue),
    }
    client = get_client(ctx)
    try:
        await client.post("issueLink", body, expect_json=False)
    except ToolError as exc:
        message = f"{exc} {WRITE_OUTCOME_UNKNOWN}"
        raise ToolError(message) from None
    return {"success": True}


async def read_remote_links(issue: str, ctx: Context) -> list[RemoteLinkPayload]:
    """Read and validate all remote links before returning any entries."""
    data = await get_client(ctx).get(f"issue/{issue}/remotelink")
    return validate_payload(RemoteLinkList, data).root


@mcp.tool(annotations=READ_ONLY)
async def get_issue_remote_links(issue: Issue, ctx: Context) -> dict[str, Any]:
    """List remote links with external object and application metadata.

    Existing URLs can use external custom schemes. Jira's own REST self URL is
    omitted; all other returned object metadata is preserved.
    """
    links = await read_remote_links(issue, ctx)
    return {"remote_links": [link.model_dump(by_alias=True, exclude_unset=True) for link in links]}


def validate_remote_url(url: str) -> None:
    """Reject unsafe or incomplete input URLs without changing their spelling."""
    message = "Remote link URL must be an absolute HTTP(S) URL with a host and no credentials or control characters."
    if any(char.isspace() or category(char) == "Cc" or char == "\\" for char in url):
        raise ToolError(message)
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
            raise ToolError(message)
        # Accessing port rejects malformed or out-of-range ports.
        parsed.port  # noqa: B018
    except ValueError:
        raise ToolError(message) from None


@mcp.tool(annotations=WRITE)
async def create_issue_remote_link(issue: Issue, url: RemoteUrl, title: RemoteTitle, ctx: Context) -> dict[str, Any]:
    """Create a remote link only when its exact URL is not already present.

    An existing match keeps its original title. The URL is never fetched or
    normalized. Preflight duplicate detection is best effort: concurrent writes
    can race. Writes are never retried. A failed or malformed write response can
    leave the outcome unknown; inspect remote links before attempting again.
    """
    validate_remote_url(url)
    links = await read_remote_links(issue, ctx)
    for link in links:
        if link.object.url == url:
            return {"created": False, "id": link.id}
    client = get_client(ctx)
    try:
        data = await client.post(f"issue/{issue}/remotelink", {"object": {"url": url, "title": title}})
        created = validate_payload(CreatedRemoteLink, data)
    except ToolError as exc:
        message = f"{exc} {WRITE_OUTCOME_UNKNOWN}"
        raise ToolError(message) from None
    return {"created": True, "id": created.id}
