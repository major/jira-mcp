"""Read Jira board configuration and saved filter scope."""

from typing import Annotated, Any

from fastmcp import Context  # noqa: TC002
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, RootModel

from jira_mcp.app import get_client, mcp
from jira_mcp.client import JiraHTTPError
from jira_mcp.common import READ_ONLY, IssueId, NonemptyString, SavedFilterPayload, validate_payload


class Board(BaseModel):
    """Validate the board metadata used in the response."""

    id: Annotated[int, Field(gt=0)]
    name: NonemptyString
    type: NonemptyString


class Identity(BaseModel):
    """Validate a Jira numeric ID before using it in a path."""

    id: IssueId


class Column(BaseModel):
    """Validate a board column and its status IDs."""

    name: NonemptyString
    statuses: list[Identity]


class ColumnConfig(BaseModel):
    """Validate the board's ordered columns."""

    columns: list[Column]


class Subquery(BaseModel):
    """Validate the optional board subquery."""

    query: str


class Configuration(BaseModel):
    """Validate the fields needed to resolve board scope."""

    filter: Identity
    column_config: ColumnConfig = Field(alias="columnConfig")
    subquery: Subquery | None = Field(default=None, alias="subQuery")


class Status(Identity):
    """Validate a status catalog entry."""

    name: NonemptyString


class StatusCatalog(RootModel[list[Status]]):
    """Validate Jira's status catalog array."""


@mcp.tool(annotations=READ_ONLY)
async def get_board(board_id: Annotated[int, Field(gt=0)], ctx: Context) -> dict[str, Any]:
    """Read board columns and saved-filter scope, not the exact board UI membership."""
    client = get_client(ctx)
    try:
        board = validate_payload(Board, await client.get_board(board_id))
        config = validate_payload(Configuration, await client.get_board(board_id, configuration=True))
    except JiraHTTPError as exc:
        if exc.status_code not in (403, 404):
            raise
        msg = "Board not found or inaccessible."
        raise ToolError(msg) from None

    filter_id = config.filter.id
    try:
        saved = validate_payload(SavedFilterPayload, await client.get(f"filter/{filter_id}"))
        board_filter = {"id": filter_id, "name": saved.name, "jql": saved.jql}
    except JiraHTTPError as exc:
        if exc.status_code not in (403, 404):
            raise
        board_filter = {
            "id": filter_id,
            "name": None,
            "jql": None,
            "unavailable_reason": "Filter not found or inaccessible.",
        }

    result: dict[str, Any] = {**board.model_dump(), "filter": board_filter}
    try:
        catalog = validate_payload(StatusCatalog, await client.get("status"))
        names = {status.id: status.name for status in catalog.root}
    except JiraHTTPError as exc:
        if exc.status_code not in (403, 404):
            raise
        names = {}
        result["status_names_unavailable"] = "Status catalog not found or inaccessible."
    result["columns"] = [
        {
            "name": column.name,
            "statuses": [{"id": status.id, "name": names.get(status.id)} for status in column.statuses],
        }
        for column in config.column_config.columns
    ]
    subquery = config.subquery.query if config.subquery is not None else None
    if subquery is not None:
        result["subquery"] = subquery
    result["jql"] = f"filter = {filter_id}" if board_filter["jql"] is not None else None
    if result["jql"] is not None and subquery and subquery.strip():
        result["jql"] += f" AND ({subquery})"
    return result
