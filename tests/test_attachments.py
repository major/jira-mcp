"""Exercise attachment tools through the MCP and Jira boundaries 📎."""

import base64
import json

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")

META = {"id": "12", "filename": "note.txt", "size": 3}


class ChunkStream(httpx2.AsyncByteStream):
    """Yield controlled chunks and record whether the response stream closes."""

    def __init__(self, chunks, *, fail=False):
        """Set the chunks and optional read failure."""
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        """Yield each chunk, then optionally fail with a sensitive transport error."""
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx2.ReadError("test-secret")

    async def aclose(self):
        """Record stream cleanup."""
        self.closed = True


@pytest.mark.parametrize("attachments", [[], [{**META, "id": 12}, {**META, "id": "13"}]])
async def test_list_attachments(mock_jira, attachments):
    """List compact metadata and normalize both integer and string IDs."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"fields": {"attachment": attachments}}))

    # Act
    result = await invoke("list_issue_attachments", {"issue": "DEMO-1"})

    # Assert
    assert not result.is_error
    assert result.structured_content == {
        "issue": "DEMO-1",
        "attachments": [{**item, "id": str(item["id"])} for item in attachments],
    }
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1"
    assert requests[0].url.params["fields"] == "attachment"


@pytest.mark.parametrize("fields", [{}, {"attachment": None}, {"attachment": {}}, {"attachment": [{"id": "1"}]}])
async def test_list_rejects_missing_or_malformed_field(mock_jira, fields):
    """Reject missing or malformed attachment fields instead of reporting an empty list."""
    # Arrange
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json={"fields": fields}))

    # Act
    result = await invoke("list_issue_attachments", {"issue": "DEMO-1"})

    # Assert
    assert result.is_error


async def test_get_attachment_compacts_metadata(mock_jira):
    """Retain optional metadata while discarding URLs and private author information."""
    # Arrange
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                **META,
                "id": 12,
                "mimeType": "text/plain",
                "created": "today",
                "self": "https://secret",
                "content": "https://secret",
                "thumbnail": "https://secret",
                "author": {
                    "accountId": "abc",
                    "displayName": "Reader",
                    "emailAddress": "secret",
                    "self": "https://secret",
                },
            },
        )
    )

    # Act
    result = await invoke("get_attachment", {"attachment_id": "12"})

    # Assert
    assert not result.is_error
    assert result.structured_content == {
        **META,
        "mime_type": "text/plain",
        "created": "today",
        "author": {"accountId": "abc", "displayName": "Reader"},
    }
    assert requests[0].url.path == "/rest/api/3/attachment/12"


@pytest.mark.parametrize(
    "metadata",
    [{**META, "id": 0}, {**META, "id": True}, {**META, "id": 12.0}, {**META, "filename": 1}, {**META, "size": -1}],
)
async def test_get_rejects_invalid_metadata(mock_jira, metadata):
    """Reject invalid required metadata returned by Jira."""
    # Arrange
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json=metadata))
    # Act
    result = await invoke("get_attachment", {"attachment_id": "12"})
    # Assert
    assert result.is_error


async def test_delete_attachment(mock_jira):
    """Delete once and report success only for HTTP 204."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(204))
    # Act
    result = await invoke("delete_attachment", {"attachment_id": "12"})
    # Assert
    assert not result.is_error
    assert result.structured_content == {"attachment_id": "12", "deleted": True}
    assert [(request.method, request.url.path) for request in requests] == [("DELETE", "/rest/api/3/attachment/12")]
    assert "x-atlassian-token" not in requests[0].headers


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(404, text="test-secret"),
        httpx2.Response(200, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
    ],
)
async def test_delete_uncertain_outcome_is_safe(mock_jira, response):
    """Do not retry failed deletes and direct callers to inspect the attachment."""
    # Arrange
    requests, responses = mock_jira
    responses.append(response)
    # Act
    result = await invoke("delete_attachment", {"attachment_id": "12"})
    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "get_attachment" in result.content[0].text
    assert "retry" in result.content[0].text
    assert [request.method for request in requests] == ["DELETE"]


@pytest.fixture
def attachment_root(jira_env, monkeypatch, tmp_path):
    """Enable local files under an isolated root with a small per-file limit."""
    monkeypatch.setenv("JIRA_ATTACHMENT_DIRECTORIES", json.dumps([str(tmp_path)]))
    monkeypatch.setenv("JIRA_ATTACHMENT_MAX_BYTES", "3")
    return tmp_path


def inline(filename, data=b"a"):
    """Make one inline file specification."""
    return {"filename": filename, "content_base64": base64.b64encode(data).decode()}


def issue_response(attachments):
    """Build an issue response containing attachment metadata."""
    return httpx2.Response(200, json={"fields": {"attachment": attachments}})


async def test_bulk_upload_validate_and_write(mock_jira, attachment_root):
    """Preflight local and inline files, then send one multipart request per file."""
    requests, responses = mock_jira
    path = attachment_root / "local.txt"
    path.write_bytes(b"abc")
    files = [{"path": str(path)}, inline("empty.txt", b"")]
    preview = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": files, "validate_only": True})
    assert not preview.is_error
    assert preview.structured_content == {
        "issue": "DEMO-1",
        "validate_only": True,
        "files": [
            {"filename": "local.txt", "size": 3},
            {"filename": "empty.txt", "size": 0},
        ],
    }
    assert requests == []
    responses.extend(
        [
            httpx2.Response(200, json=[{**META, "filename": "local.txt"}]),
            httpx2.Response(200, json=[{**META, "id": 13, "filename": "empty.txt", "size": 0}]),
        ]
    )
    result = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": files})
    assert not result.is_error
    assert result.structured_content == {
        "issue": "DEMO-1",
        "validate_only": False,
        "attachments": [
            {**META, "filename": "local.txt"},
            {**META, "id": "13", "filename": "empty.txt", "size": 0},
        ],
        "failed": [],
    }
    assert len(requests) == len(files)
    for request, filename, data in zip(requests, (b"local.txt", b"empty.txt"), (b"abc", b""), strict=True):
        assert request.method == "POST"
        assert request.url.path == "/rest/api/3/issue/DEMO-1/attachments"
        assert request.headers["x-atlassian-token"] == "no-check"
        assert request.content.count(b'Content-Disposition: form-data; name="file"') == 1
        assert b'filename="' + filename + b'"' in request.content
        assert b"\r\n\r\n" + data + b"\r\n" in request.content


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"path": "/missing", "filename": "x", "content_base64": "YQ=="},
        {"filename": "", "content_base64": ""},
        {"filename": "../x", "content_base64": ""},
        {"filename": "a\\b", "content_base64": ""},
        {"filename": "a\nx", "content_base64": ""},
        {"filename": ".", "content_base64": ""},
        {"filename": "..", "content_base64": ""},
        {"filename": "a" * 256, "content_base64": ""},
        {"filename": "x", "content_base64": "!!!!"},
        {"filename": "x", "content_base64": "A" * 1398105},
        inline("x", b"abcd"),
    ],
)
async def test_upload_invalid_last_file_has_no_posts(mock_jira, attachment_root, bad):
    """Reject the entire batch before posting when any file is invalid."""
    requests, _ = mock_jira
    result = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": [inline("ok"), bad]})
    assert result.is_error
    assert requests == []


@pytest.mark.parametrize("files", [[], [inline("x")] * 21, [inline("x"), inline("x")]])
async def test_upload_batch_bounds_and_duplicates(mock_jira, attachment_root, files):
    """Reject empty, too large, and duplicate-name batches before Jira writes."""
    requests, _ = mock_jira
    result = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": files})
    assert result.is_error
    assert requests == []


@pytest.mark.parametrize("kind", ["missing", "directory", "leaf_symlink", "ancestor_symlink", "dotdot", "outside"])
async def test_upload_unsafe_paths(mock_jira, attachment_root, kind):
    """Never read missing, nonregular, traversing, outside, or symlinked paths."""
    requests, _ = mock_jira
    target = attachment_root / "target"
    target.write_bytes(b"a")
    directory = attachment_root / "dir"
    directory.mkdir()
    (directory / "file").write_bytes(b"a")
    link = attachment_root / "link"
    link.symlink_to(directory if kind == "ancestor_symlink" else target)
    paths = {
        "missing": attachment_root / "missing",
        "directory": directory,
        "leaf_symlink": link,
        "ancestor_symlink": link / "file",
        "dotdot": attachment_root / "dir" / ".." / "target",
        "outside": attachment_root.parent / "outside",
    }
    result = await invoke(
        "add_issue_attachments", {"issue": "DEMO-1", "files": [inline("ok"), {"path": str(paths[kind])}]}
    )
    assert result.is_error
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, json=[]),
        httpx2.Response(200, json=[META, META]),
        httpx2.Response(403, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
    ],
)
async def test_upload_failure_continues_without_retry(mock_jira, attachment_root, response):
    """Report safe uncertain outcomes and continue with subsequent files."""
    requests, responses = mock_jira
    responses.extend([response, httpx2.Response(200, json=[{**META, "filename": "next"}])])
    result = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": [inline("first"), inline("next")]})
    assert not result.is_error
    data = result.structured_content
    assert data["failed"][0]["filename"] == "first"
    assert "test-secret" not in str(data)
    assert "list_issue_attachments" in str(data)
    assert "retry" in str(data).lower()
    assert data["attachments"][0]["filename"] == "next"
    assert [r.method for r in requests] == ["POST", "POST"]


@pytest.mark.parametrize("chunks", [[], [b"ab", b"c"]])
async def test_download_streams_and_closes(mock_jira, attachment_root, chunks):
    """Write exact-limit and empty downloads without exposing content in results."""
    requests, responses = mock_jira
    stream = ChunkStream(chunks)
    data = b"".join(chunks)
    responses.extend(
        [issue_response([{**META, "size": len(data), "mimeType": "text/plain"}]), httpx2.Response(200, stream=stream)]
    )
    result = await invoke("download_issue_attachments", {"issue": "DEMO-1", "directory": str(attachment_root)})
    assert not result.is_error
    assert result.structured_content == {
        "issue": "DEMO-1",
        "downloaded": [
            {
                "filename": "note.txt",
                "id": "12",
                "path": str(attachment_root / "note.txt"),
                "bytes": len(data),
                "mime_type": "text/plain",
            }
        ],
        "failed": [],
    }
    assert (attachment_root / "note.txt").read_bytes() == data
    assert stream.closed
    request = requests[1]
    assert request.url.path == "/rest/api/3/attachment/content/12"
    assert request.url.params["redirect"] == "false"
    assert request.headers["accept"] == "*/*"
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("status", "headers", "chunks", "fail"),
    [
        (206, {}, [b"a"], False),
        (302, {"location": "https://other.invalid"}, [], False),
        (200, {"content-encoding": "gzip"}, [b"a"], False),
        (200, {"content-length": "1"}, [b"ab", b"cd"], False),
        (200, {}, [b"a"], True),
    ],
)
async def test_download_failed_stream_preserves_existing(mock_jira, attachment_root, status, headers, chunks, fail):  # noqa: PLR0913, PLR0917
    """Close unsafe streams and preserve existing files without temporary residue."""
    requests, responses = mock_jira
    destination = attachment_root / "note.txt"
    destination.write_bytes(b"old")
    stream = ChunkStream(chunks, fail=fail)
    responses.extend([issue_response([META]), httpx2.Response(status, headers=headers, stream=stream)])
    result = await invoke(
        "download_issue_attachments", {"issue": "DEMO-1", "directory": str(attachment_root), "overwrite": True}
    )
    assert not result.is_error
    assert result.structured_content["downloaded"] == []
    assert len(result.structured_content["failed"]) == 1
    assert "test-secret" not in str(result.structured_content)
    assert destination.read_bytes() == b"old"
    assert list(attachment_root.iterdir()) == [destination]
    assert stream.closed
    assert [request.method for request in requests] == ["GET", "GET"]


@pytest.mark.parametrize("case", ["oversized", "unsafe", "duplicate", "existing", "symlink", "nonregular"])
async def test_download_rejects_file_before_content_get(mock_jira, attachment_root, case):
    """Skip unsafe metadata and destinations without requesting content."""
    requests, responses = mock_jira
    metadata = [META]
    destination = attachment_root / "note.txt"
    if case == "oversized":
        metadata = [{**META, "size": 4}]
    elif case == "unsafe":
        metadata = [{**META, "filename": "../escape"}]
    elif case == "duplicate":
        metadata = [META, {**META, "id": "13"}]
    elif case == "existing":
        destination.write_bytes(b"old")
    elif case == "symlink":
        destination.symlink_to(attachment_root / "target")
    else:
        destination.mkdir()
    responses.append(issue_response(metadata))
    result = await invoke(
        "download_issue_attachments",
        {
            "issue": "DEMO-1",
            "directory": str(attachment_root),
            "overwrite": case in {"duplicate", "symlink", "nonregular"},
        },
    )
    assert not result.is_error
    assert result.structured_content["downloaded"] == []
    assert len(result.structured_content["failed"]) == len(metadata)
    assert len(requests) == 1
    if case == "existing":
        assert destination.read_bytes() == b"old"


async def test_download_mixed_selection_and_overwrite(mock_jira, attachment_root):
    """Replace regular files and report missing selections while continuing the batch."""
    requests, responses = mock_jira
    destination = attachment_root / "note.txt"
    destination.write_bytes(b"old")
    responses.extend([issue_response([META]), httpx2.Response(200, stream=ChunkStream([b"new"]))])
    result = await invoke(
        "download_issue_attachments",
        {
            "issue": "DEMO-1",
            "directory": str(attachment_root),
            "filenames": ["missing.txt", "note.txt"],
            "overwrite": True,
        },
    )
    assert not result.is_error
    assert destination.read_bytes() == b"new"
    assert result.structured_content["downloaded"][0]["filename"] == "note.txt"
    assert result.structured_content["failed"][0]["filename"] == "missing.txt"
    assert [request.method for request in requests] == ["GET", "GET"]
    assert len(list(attachment_root.iterdir())) == 1


@pytest.mark.parametrize("case", ["dotdot", "outside", "prefix", "symlink"])
async def test_download_unsafe_directory(mock_jira, attachment_root, case):
    """Reject directory traversal, prefix collisions, and symlinked directories."""
    requests, _ = mock_jira
    link = attachment_root / "link"
    link.symlink_to(attachment_root, target_is_directory=True)
    paths = {
        "dotdot": attachment_root / ".." / "escape",
        "outside": attachment_root.parent / "escape",
        "prefix": attachment_root.parent / (attachment_root.name + "-escape"),
        "symlink": link / "nested",
    }
    result = await invoke("download_issue_attachments", {"issue": "DEMO-1", "directory": str(paths[case])})
    assert result.is_error
    assert not paths[case].exists()
    assert not any("/attachment/content/" in str(request.url) for request in requests)


async def test_download_empty_selection_rejected(mock_jira, attachment_root):
    """Reject an empty filename selection rather than downloading everything."""
    requests, _ = mock_jira
    result = await invoke(
        "download_issue_attachments", {"issue": "DEMO-1", "directory": str(attachment_root), "filenames": []}
    )
    assert result.is_error
    assert requests == []


async def test_local_io_disabled_by_default(mock_jira, tmp_path):
    """Require an explicit root for local upload and download paths."""
    requests, _ = mock_jira
    path = tmp_path / "file"
    path.write_bytes(b"a")
    upload = await invoke("add_issue_attachments", {"issue": "DEMO-1", "files": [{"path": str(path)}]})
    download = await invoke("download_issue_attachments", {"issue": "DEMO-1", "directory": str(tmp_path)})
    assert upload.is_error
    assert download.is_error
    assert not any(request.method == "POST" or "/attachment/content/" in str(request.url) for request in requests)
