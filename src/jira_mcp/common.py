"""Share argument types, tool annotations, and payload helpers across tool modules."""

import json
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

INVALID_PAYLOAD = "Jira returned an unexpected response shape. Check the Jira Cloud URL and API access."

Issue = Annotated[str, Field(pattern=r"^(?:[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*|[1-9][0-9]*)$", max_length=255)]
IssueId = Annotated[str, Field(pattern=r"^[1-9][0-9]*$", max_length=255)]
Offset = Annotated[int, Field(ge=0)]
PageSize = Annotated[int, Field(ge=1, le=100)]
NonemptyString = Annotated[str, Field(min_length=1)]
AccountId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
CREATE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
WRITE_OUTCOME_UNKNOWN = "The write outcome may be unknown; inspect the issue or links before retrying."


class IssuePayload(BaseModel):
    """Validate the small portion of an issue response used by this server."""

    id: str
    key: str
    fields: dict[str, Any]
    names: dict[str, str] = Field(default_factory=dict)
    field_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


def validate_payload[T: BaseModel](model: type[T], data: Any) -> T:
    """Turn malformed upstream data into a safe, actionable tool error."""
    try:
        return model.model_validate(data)
    except ValidationError:
        raise ToolError(INVALID_PAYLOAD) from None


def next_offset(start: int, count: int, total: int) -> int | None:
    """Stop on empty pages even if Jira's mutable total still advertises results."""
    end = start + count
    return end if count and end < total else None


def id_or_key(value: str) -> dict[str, str]:
    """Reference a Jira project or issue by numeric ID or by key."""
    return {"id" if value.isdigit() else "key": value}


Summary = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\r\n]*\S[^\r\n]*$")]


class SavedFilterPayload(BaseModel):
    """Keep only useful saved filter fields."""

    id: IssueId
    name: Summary
    jql: str | None = None
    view_url: str | None = Field(default=None, alias="viewUrl")


Description = Annotated[str, Field(max_length=100_000)]


Label = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]


Labels = Annotated[list[Label], Field(max_length=100)]


ADF_MAX_BYTES = 100_000


def validate_description_adf(adf: dict[str, Any]) -> dict[str, Any]:
    """Validate the ADF envelope and compact UTF-8 JSON size without changing its content."""
    if (
        adf.get("type") != "doc"
        or type(adf.get("version")) is not int
        or adf["version"] != 1
        or not isinstance(adf.get("content"), list)
    ):
        msg = "Invalid description_adf: expected an ADF document envelope."
        raise ToolError(msg)
    try:
        size = len(json.dumps(adf, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except TypeError, ValueError, UnicodeError, RecursionError:
        msg = "Invalid description_adf: expected serializable JSON."
        raise ToolError(msg) from None
    if size > ADF_MAX_BYTES:
        msg = "Invalid description_adf: compact UTF-8 JSON exceeds 100,000 bytes."
        raise ToolError(msg)
    return adf
