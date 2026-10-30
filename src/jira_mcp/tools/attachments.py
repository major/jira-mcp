"""Read and explicitly change small Jira attachments."""

import base64
import binascii
import io
from collections import Counter
from contextlib import ExitStack
from typing import Annotated, Any

from fastmcp import Context  # noqa: TC002
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from jira_mcp.app import get_client, mcp
from jira_mcp.attachment_files import validate_filename
from jira_mcp.common import CREATE, READ_ONLY, WRITE, Issue, IssueId, validate_payload

MAX_BYTES = 1_048_576
MAX_ENCODED = 1_398_104
MAX_TOTAL = 104_857_600


class AttachmentAuthor(BaseModel):
    """Retain only public author identity fields."""

    accountId: StrictStr | None = None  # noqa: N815
    displayName: StrictStr | None = None  # noqa: N815


class Attachment(BaseModel):
    """Validate and retain only compact attachment metadata."""

    id: IssueId
    filename: StrictStr
    size: Annotated[StrictInt, Field(ge=0)]
    mime_type: StrictStr | None = Field(default=None, validation_alias="mimeType")
    created: StrictStr | StrictInt | None = None
    author: AttachmentAuthor | None = None

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> Any:
        """Accept only integer or string IDs, not booleans or floats."""
        if type(value) is int:
            return str(value)
        return value


class AttachmentFields(BaseModel):
    """Require the attachment field on an issue response."""

    attachment: list[Attachment]


class AttachmentIssue(BaseModel):
    """Require an issue fields envelope."""

    fields: AttachmentFields


class UploadResult(BaseModel):
    """Require exactly one attachment in an upload acknowledgment."""

    attachments: Annotated[list[Attachment], Field(min_length=1, max_length=1)]


@mcp.tool(annotations=READ_ONLY)
async def list_issue_attachments(issue: Issue, ctx: Context) -> dict[str, Any]:
    """List compact attachment metadata without downloading content."""
    data = await get_client(ctx).get(f"issue/{issue}", params={"fields": "attachment"})
    result = validate_payload(AttachmentIssue, data)
    return {
        "issue": issue,
        "attachments": [item.model_dump(exclude_unset=True, by_alias=True) for item in result.fields.attachment],
    }


@mcp.tool(annotations=READ_ONLY)
async def get_attachment(attachment_id: IssueId, ctx: Context) -> dict[str, Any]:
    """Get compact metadata for one attachment without downloading content."""
    data = await get_client(ctx).get(f"attachment/{attachment_id}")
    return validate_payload(Attachment, data).model_dump(exclude_unset=True, by_alias=True)


class UploadFile(BaseModel):
    """Describe a server-local path or inline file without accepting extra keys."""

    model_config = ConfigDict(extra="forbid")
    path: StrictStr | None = None
    filename: StrictStr | None = None
    content_base64: StrictStr | None = None


@mcp.tool(annotations=CREATE)
async def add_issue_attachments(  # noqa: C901
    issue: Issue,
    files: Annotated[list[UploadFile], Field(min_length=1, max_length=20)],
    ctx: Context,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Snapshot all files before upload; check the issue before retrying a failed or cancelled write."""
    client = get_client(ctx)
    storage = ctx.lifespan_context["attachment_storage"]
    with ExitStack() as stack:
        snapshots = []
        names = set()
        total = 0
        for file in files:
            if file.path is not None and file.model_fields_set == {"path"}:
                filename, content, size = storage.open_upload(file.path)
                stack.callback(content.close)
            elif (
                file.model_fields_set == {"filename", "content_base64"}
                and file.filename is not None
                and file.content_base64 is not None
            ):
                filename = file.filename
                validate_filename(filename)
                if len(file.content_base64) > MAX_ENCODED:
                    msg = "Inline attachment exceeds the base64 limit"
                    raise ToolError(msg)
                try:
                    data = base64.b64decode(file.content_base64, validate=True)
                except binascii.Error, ValueError:
                    msg = "Invalid base64 attachment content"
                    raise ToolError(msg) from None
                size = len(data)
                if size > min(MAX_BYTES, storage.max_bytes):
                    msg = "Attachment exceeds the upload byte limit"
                    raise ToolError(msg)
                content = stack.enter_context(io.BytesIO(data))
            else:
                msg = "Provide either a path or a filename and base64 content"
                raise ToolError(msg)
            if filename in names:
                msg = "Duplicate attachment filename"
                raise ToolError(msg)
            names.add(filename)
            total += size
            if total > MAX_TOTAL:
                msg = "Attachments exceed the aggregate byte limit"
                raise ToolError(msg)
            snapshots.append((filename, content, size))
        if validate_only:
            return {
                "issue": issue,
                "validate_only": True,
                "files": [{"filename": n, "size": size} for n, _, size in snapshots],
            }
        attachments = []
        failed = []
        for filename, content, _ in snapshots:
            try:
                data = await client.upload_attachment(issue, filename, content)
                result = validate_payload(UploadResult, {"attachments": data})
                attachments.append(result.attachments[0].model_dump(exclude_unset=True))
            except ToolError as exc:
                failed.append(
                    {
                        "filename": filename,
                        "reason": (
                            f"{exc}. The attachment may already exist; check list_issue_attachments before retrying."
                        ),
                        "outcome_unknown": True,
                    }
                )
        return {"issue": issue, "validate_only": False, "attachments": attachments, "failed": failed}


@mcp.tool(annotations=WRITE)
async def download_issue_attachments(
    issue: Issue,
    directory: str,
    ctx: Context,
    filenames: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Save bounded attachments to a server directory without returning their contents."""
    client = get_client(ctx)
    storage = ctx.lifespan_context["attachment_storage"]
    downloaded = []
    failed = []
    with storage.open_directory(directory) as target:
        data = await client.get(f"issue/{issue}", params={"fields": "attachment"})
        items = validate_payload(AttachmentIssue, data).fields.attachment
        counts = Counter(item.filename for item in items)
        if filenames is not None:
            available = {item.filename for item in items}
            failed.extend(
                {"filename": name, "reason": "Attachment not found"} for name in filenames if name not in available
            )
            items = [item for item in items if item.filename in filenames]
        for item in items:
            try:
                validate_filename(item.filename)
                if counts[item.filename] > 1:
                    msg = "Duplicate attachment filename"
                    raise ToolError(msg)  # noqa: TRY301
                if item.size > storage.max_bytes:
                    msg = "Attachment exceeds the download byte limit"
                    raise ToolError(msg)  # noqa: TRY301
                target.check_destination(item.filename, overwrite=overwrite)
                content = await client.download_attachment(item.id, storage.max_bytes)
                if len(content) != item.size:
                    msg = "Attachment content size does not match its metadata"
                    raise ToolError(msg)  # noqa: TRY301
                path = target.write(item.filename, content, overwrite=overwrite)
                downloaded.append(
                    {
                        "filename": item.filename,
                        "id": item.id,
                        "path": path,
                        "bytes": len(content),
                        "mime_type": item.mime_type,
                    }
                )
            except ToolError as exc:
                failed.append({"filename": item.filename, "id": item.id, "reason": str(exc)})
    return {"issue": issue, "downloaded": downloaded, "failed": failed}


@mcp.tool(annotations=WRITE)
async def delete_attachment(attachment_id: IssueId, ctx: Context) -> dict[str, Any]:
    """Irreversibly delete one attachment; check its status before retrying on failure or cancellation."""
    try:
        await get_client(ctx).delete_attachment(attachment_id)
    except ToolError as exc:
        msg = f"{exc}. The attachment may already be deleted; check get_attachment before retrying."
        raise ToolError(msg) from None
    return {"attachment_id": attachment_id, "deleted": True}
