"""Exercise attachment tools through the MCP and Jira boundaries 📎."""

import base64

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
    """Retain optional metadata while discarding URLs and author information."""
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
                "author": {"self": "https://secret"},
            },
        )
    )

    # Act
    result = await invoke("get_attachment", {"attachment_id": "12"})

    # Assert
    assert not result.is_error
    assert result.structured_content == {**META, "mimeType": "text/plain", "created": "today"}
    assert requests[0].url.path == "/rest/api/3/attachment/12"


@pytest.mark.parametrize("metadata", [{**META, "id": 0}, {**META, "filename": 1}, {**META, "size": -1}])
async def test_get_rejects_invalid_metadata(mock_jira, metadata):
    """Reject invalid required metadata returned by Jira."""
    # Arrange
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json=metadata))
    # Act
    result = await invoke("get_attachment", {"attachment_id": "12"})
    # Assert
    assert result.is_error


@pytest.mark.parametrize("chunks", [[], [b"ab", b"c"]])
async def test_get_content_streams_and_closes(mock_jira, chunks):
    """Return the whole file, including empty files, and close its stream."""
    # Arrange
    requests, responses = mock_jira
    stream = ChunkStream(chunks)
    responses.append(httpx2.Response(200, stream=stream))
    # Act
    result = await invoke("get_attachment_content", {"attachment_id": "12"})
    # Assert
    data = b"".join(chunks)
    assert not result.is_error
    assert result.structured_content == {
        "attachment_id": "12",
        "encoding": "base64",
        "size": len(data),
        "content_base64": base64.b64encode(data).decode(),
    }
    assert stream.closed
    assert requests[0].url.path == "/rest/api/3/attachment/content/12"
    assert requests[0].url.params["redirect"] == "false"
    assert requests[0].headers["accept"] == "*/*"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert len(requests) == 1


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
async def test_get_content_rejects_unsafe_streams(mock_jira, status, headers, chunks, fail):
    """Reject partial, redirected, encoded, oversized, and failed streams safely."""
    # Arrange
    requests, responses = mock_jira
    stream = ChunkStream(chunks, fail=fail)
    responses.append(httpx2.Response(status, headers=headers, stream=stream))
    # Act
    result = await invoke("get_attachment_content", {"attachment_id": "12", "max_bytes": 3})
    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert stream.closed
    assert len(requests) == 1


@pytest.mark.parametrize("data", [b"", b"abc"])
async def test_upload_multipart(mock_jira, data):
    """Upload exactly one file part and return compact metadata."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=[{**META, "size": len(data), "self": "https://secret"}]))
    # Act
    result = await invoke(
        "add_issue_attachment",
        {"issue": "DEMO-1", "filename": "note.txt", "content_base64": base64.b64encode(data).decode()},
    )
    # Assert
    assert not result.is_error
    assert result.structured_content == {"issue": "DEMO-1", "attachment": {**META, "size": len(data)}}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/api/3/issue/DEMO-1/attachments"
    assert request.headers["x-atlassian-token"] == "no-check"
    assert request.content.count(b'Content-Disposition: form-data; name="file"') == 1
    assert b'filename="note.txt"' in request.content
    assert b"\r\n\r\n" + data + b"\r\n" in request.content


@pytest.mark.parametrize(
    "args",
    [
        {"issue": "../bad"},
        {"filename": "../x"},
        {"filename": "a\\b"},
        {"filename": "a\nx"},
        {"filename": "."},
        {"filename": ".."},
        {"filename": "a" * 256},
        {"content_base64": "a_=="},
        {"content_base64": "!!!!"},
        {"content_base64": "A" * 1_398_105},
        {"content_base64": base64.b64encode(b"a" * 1_048_577).decode()},
    ],
)
async def test_upload_invalid_arguments_do_not_request(mock_jira, args):
    """Reject invalid identifiers, filenames, base64, and size before a request."""
    # Arrange
    requests, _ = mock_jira
    arguments = {"issue": "DEMO-1", "filename": "note.txt", "content_base64": "YQ==", **args}
    # Act
    result = await invoke("add_issue_attachment", arguments)
    # Assert
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
async def test_upload_uncertain_outcome_is_safe(mock_jira, response):
    """Do not retry upload failures and direct callers to inspect the issue first."""
    # Arrange
    requests, responses = mock_jira
    responses.append(response)
    # Act
    result = await invoke("add_issue_attachment", {"issue": "DEMO-1", "filename": "note.txt", "content_base64": "YQ=="})
    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "list_issue_attachments" in result.content[0].text
    assert "retry" in result.content[0].text
    assert [request.method for request in requests] == ["POST"]


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


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_attachment", {"attachment_id": "0"}),
        ("get_attachment_content", {"attachment_id": "../1"}),
        ("delete_attachment", {"attachment_id": "-1"}),
        ("get_attachment_content", {"attachment_id": "1", "max_bytes": 0}),
        ("get_attachment_content", {"attachment_id": "1", "max_bytes": 1_048_577}),
    ],
)
async def test_invalid_read_and_delete_arguments_do_not_request(mock_jira, tool, args):
    """Reject invalid attachment IDs and content limits before contacting Jira."""
    # Arrange
    requests, _ = mock_jira
    # Act
    result = await invoke(tool, args)
    # Assert
    assert result.is_error
    assert requests == []
