"""Validate Jira create metadata and check planned issue fields against it."""

import re
from typing import Any, Self

from pydantic import BaseModel, Field, StrictBool, model_validator

from jira_mcp.common import IssueId

UNLISTED_CREATE_FIELDS = frozenset({"project", "issuetype", "parent"})


SAFE_FIELD_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}")


class CreateIssueType(BaseModel):
    """Keep the identity and useful details of one creatable issue type."""

    id: IssueId
    name: str = Field(min_length=1)
    description: str | None = None
    subtask: StrictBool | None = None


class CreateField(BaseModel):
    """Keep field identities and original values needed to understand creation."""

    field_id: str | None = Field(default=None, alias="fieldId", min_length=1)
    key: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    required: StrictBool
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    operations: list[str] | None = None
    has_default_value: StrictBool | None = Field(default=None, alias="hasDefaultValue")
    default_value: Any = Field(default=None, alias="defaultValue")
    allowed_values: list[Any] | None = Field(default=None, alias="allowedValues")
    auto_complete_url: str | None = Field(default=None, alias="autoCompleteUrl")

    @model_validator(mode="after")
    def require_identity(self) -> Self:
        """Require at least one field identity without relying on the name."""
        if self.field_id is None and self.key is None:
            raise ValueError
        return self


class CreateMetadataPage(BaseModel):
    """Validate Jira's offset-based create-metadata envelope."""

    start_at: int = Field(alias="startAt", ge=0, strict=True)
    max_results: int = Field(alias="maxResults", ge=0, strict=True)
    total: int = Field(ge=0, strict=True)


class CreateIssueTypePage(CreateMetadataPage):
    """Validate one page of project issue types."""

    issue_types: list[CreateIssueType] = Field(alias="issueTypes")


class CreateFieldPage(CreateMetadataPage):
    """Validate one page of create-field metadata."""

    fields: list[CreateField]


def safe_field_id(field: CreateField) -> str:
    """Name a metadata field by ID only, because names and odd IDs are untrusted text."""
    field_id = field.field_id or field.key or ""
    return field_id if SAFE_FIELD_ID.fullmatch(field_id) else "a field with an unusual ID"


def option_allowed(item: Any, allowed: list[dict[str, Any]]) -> bool:
    """Match one option on the first identity Jira would use, ignoring case and type."""
    if not isinstance(item, dict):
        return True
    for identity in ("id", "value", "name"):
        choices = [str(option[identity]).casefold() for option in allowed if identity in option]
        if identity in item and choices:
            return str(item[identity]).casefold() in choices
    return True


def value_allowed(value: Any, allowed: list[Any] | None) -> bool:
    """Check option values leniently so local validation is never stricter than Jira.

    Skip empty or non-object option lists and free-form values. Cascading selects
    are checked only at the top level because child options are not listed here.
    """
    if not allowed or not all(isinstance(option, dict) for option in allowed):
        return True
    return all(option_allowed(item, allowed) for item in (value if isinstance(value, list) else [value]))


def create_field_problems(fields: dict[str, Any], metadata: list[CreateField]) -> list[str]:
    """List missing, unavailable, and disallowed fields using only safe field IDs."""
    if not metadata:
        return ["Jira returned no creation fields; check the issue type ID and the Create issues permission"]
    known = {identity: field for field in metadata for identity in (field.field_id, field.key) if identity}
    problems = [
        f"{safe_field_id(field)} is required"
        for field in metadata
        if field.required and not field.has_default_value and not {field.field_id, field.key} & fields.keys()
    ]
    for field_id, value in fields.items():
        field = known.get(field_id)
        # Parent may be settable even when the create screen does not list it.
        if field is None and field_id not in UNLISTED_CREATE_FIELDS:
            problems.append(f"{field_id} is not available when creating this issue type")
        # Project and issue type options still pass: Jira lists the selected one, and keys are not checked.
        # Autocomplete fields may list only some valid options, so their values are left to Jira.
        elif field is not None and field.auto_complete_url is None and not value_allowed(value, field.allowed_values):
            problems.append(f"{field_id} has a value that is not allowed")
    return problems
