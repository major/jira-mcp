"""Exercise the comment write tool through the MCP and Jira boundaries 💬."""

import copy
import hashlib
import json

import httpx2
import pytest

from jira_mcp.text import markdown_to_adf
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")

BODY = "## Fixed\n3. [tests](https://example.com)\n4. `main`"
VISIBILITY = {"type": "role", "value": "Developers"}


async def test_add_comment_posts_adf_once_and_returns_identity(mock_jira):
    """Send one POST with converted ADF and visibility, then report the new comment."""
    # Arrange
    requests, responses = mock_jira
    created = {"id": "10042", "created": "2026-10-29T12:00:00.000+0000", "self": "https://jira.invalid/c/10042"}
    responses.append(httpx2.Response(201, json=created))

    # Act
    result = await invoke("add_issue_comment", {"issue": "DEMO-1", "body": BODY, "visibility": VISIBILITY})

    # Assert
    assert not result.is_error
    assert result.structured_content == {"id": "10042", "issue": "DEMO-1", "created": created["created"]}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1/comment"
    assert json.loads(requests[0].content) == {"body": markdown_to_adf(BODY), "visibility": VISIBILITY}


async def test_validate_only_returns_adf_without_requests(mock_jira, monkeypatch):
    """Preview the exact payload without a request, even before Jira is configured."""
    # Arrange
    requests, _ = mock_jira
    monkeypatch.delenv("JIRA_API_TOKEN")

    # Act
    result = await invoke("add_issue_comment", {"issue": "DEMO-1", "body": BODY, "validate_only": True})

    # Assert
    assert not result.is_error
    assert result.structured_content == {"validate_only": True, "issue": "DEMO-1", "adf": markdown_to_adf(BODY)}
    assert requests == []


async def test_preview_contains_emphasis_marks(mock_jira):
    """Expose bold and italic ADF marks in the public comment preview."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke(
        "add_issue_comment", {"issue": "DEMO-1", "body": "**bold** and _italic_", "validate_only": True}
    )

    # Assert
    assert not result.is_error
    nodes = result.structured_content["adf"]["content"][0]["content"]
    assert nodes == [
        {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
        {"type": "text", "text": " and "},
        {"type": "text", "text": "italic", "marks": [{"type": "em"}]},
    ]
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
        httpx2.Response(201, json={"id": "test-secret"}),
    ],
)
async def test_comment_write_errors_are_safe_and_not_retried(mock_jira, response):
    """Hide upstream details, make one attempt, and point at the comment list before a retry."""
    # Arrange
    requests, responses = mock_jira
    responses.append(response)

    # Act
    result = await invoke("add_issue_comment", {"issue": "DEMO-1", "body": BODY})

    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "get_issue_comments before retrying" in result.content[0].text
    assert [request.method for request in requests] == ["POST"]


@pytest.mark.parametrize(
    "args",
    [
        {"issue": "../bad", "body": "text"},
        {"issue": "DEMO-1", "body": " \n\t"},
        {"issue": "DEMO-1", "body": "x" * 32_768},
        {"issue": "DEMO-1", "body": "text", "visibility": {"type": "user", "value": "me"}},
        {"issue": "DEMO-1", "body": "text", "visibility": {"type": "group", "value": ""}},
    ],
)
async def test_invalid_comment_arguments_do_not_request(mock_jira, args):
    """Reject bad issue keys, blank or oversized bodies, and bad visibility before any request."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke("add_issue_comment", args)

    # Assert
    assert result.is_error
    assert requests == []


@pytest.fixture
def edit_document():
    """Provide rich ADF with fields that an edit must preserve."""
    return {
        "type": "doc",
        "version": 1,
        "extra": {"text": "café"},
        "content": [
            {
                "type": "paragraph",
                "attrs": {"id": "café"},
                "content": [
                    {"type": "text", "text": "café and café", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "café", "unknown": {"text": "café"}},
                    {"type": "emoji", "attrs": {"shortName": ":café:"}},
                ],
            },
        ],
    }


def document_hash(document):
    """Hash the complete canonical UTF-8 document."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("preview", [True, False])
async def test_edit_replaces_all_text_nodes_and_preserves_document(mock_jira, edit_document, preview):
    """Replace nonoverlapping matches without changing marks, attributes, or unknown fields."""
    # Arrange
    requests, responses = mock_jira
    expected = copy.deepcopy(edit_document)
    expected["content"][0]["content"][0]["text"] = "tea and tea"
    expected["content"][0]["content"][1]["text"] = "tea"
    responses.extend([httpx2.Response(200, json={"body": edit_document}), httpx2.Response(200, text="not json")])

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "old_text": "café",
            "new_text": "tea",
            "replace_all": True,
            "validate_only": preview,
            "expected_hash": document_hash(edit_document),
        },
    )

    # Assert
    assert not result.is_error
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1/comment/42"
    assert [request.method for request in requests] == (["GET"] if preview else ["GET", "PUT"])
    if preview:
        assert result.structured_content["adf"] == expected
        assert result.structured_content["before_hash"] == document_hash(edit_document)
        assert result.structured_content["after_hash"] == document_hash(expected)
        assert isinstance(result.structured_content["diff"], str)
        assert isinstance(result.structured_content["diff_truncated"], bool)
        assert result.structured_content["validate_only"] is True
    else:
        assert json.loads(requests[1].content) == {"body": expected}
        assert result.structured_content == {
            "issue": "DEMO-1",
            "comment_id": "42",
            "updated": True,
            "body_hash": document_hash(expected),
        }


@pytest.mark.parametrize("old_text", ["café and café", "café"])
async def test_edit_rejects_ambiguous_matches(mock_jira, edit_document, old_text):
    """Reject repeated matches within one node or across nodes without replace_all."""
    # Arrange
    requests, responses = mock_jira
    if old_text == "café and café":
        edit_document["content"][0]["content"][0]["text"] = "café and café café and café"
    responses.append(httpx2.Response(200, json={"body": edit_document}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "old_text": old_text,
            "new_text": "tea",
        },
    )

    # Assert
    assert result.is_error
    assert [request.method for request in requests] == ["GET"]


async def test_edit_rejects_match_spanning_nodes(mock_jira, edit_document):
    """Do not match text that exists only across separate ADF text nodes."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"body": edit_document}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "old_text": "café and cafécafé",
            "new_text": "x",
        },
    )

    # Assert
    assert result.is_error
    assert "not found in a single text node" in result.content[0].text
    assert [request.method for request in requests] == ["GET"]


async def test_edit_deletes_empty_text_node(mock_jira, edit_document):
    """Remove an emptied text node while preserving its siblings and parent."""
    # Arrange
    requests, responses = mock_jira
    expected = copy.deepcopy(edit_document)
    expected["content"][0]["content"][0]["text"] = " and "
    del expected["content"][0]["content"][1]
    responses.append(httpx2.Response(200, json={"body": edit_document}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "old_text": "café",
            "new_text": "",
            "replace_all": True,
            "validate_only": True,
        },
    )

    # Assert
    assert not result.is_error
    assert result.structured_content["adf"] == expected
    assert [request.method for request in requests] == ["GET"]


async def test_edit_full_body_converts_markdown(mock_jira, edit_document):
    """Replace the whole document with converted Markdown in one PUT."""
    # Arrange
    requests, responses = mock_jira
    responses.extend([httpx2.Response(200, json={"body": edit_document}), httpx2.Response(204)])

    # Act
    result = await invoke("edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", "body": BODY})

    # Assert
    assert not result.is_error
    assert [request.method for request in requests] == ["GET", "PUT"]
    assert json.loads(requests[1].content) == {"body": markdown_to_adf(BODY)}


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"body": "x", "old_text": "a", "new_text": "b"},
        {"old_text": "", "new_text": "x"},
        {"old_text": "x"},
        {"new_text": "x"},
        {"body": "x", "replace_all": True},
        {"body": "x", "expected_hash": "A" * 64},
        {"body": "x", "expected_hash": "bad"},
    ],
)
async def test_edit_invalid_arguments_do_not_request(mock_jira, args):
    """Reject invalid edit modes and hash syntax locally."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke("edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", **args})

    # Assert
    assert result.is_error
    assert requests == []


@pytest.mark.parametrize(
    ("body", "expected_hash"),
    [
        ({"type": "doc", "version": 1, "content": []}, "0" * 64),
        ({"type": "paragraph", "content": []}, None),
        ("not ADF", None),
    ],
)
async def test_edit_rejects_hash_mismatch_or_malformed_source(mock_jira, body, expected_hash):
    """Reject a stale guard or invalid ADF without sending a write."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"body": body}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "body": "new",
            "expected_hash": expected_hash,
        },
    )

    # Assert
    assert result.is_error
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("response", "uncertain"),
    [
        (httpx2.Response(403, text="test-secret"), False),
        (httpx2.ReadTimeout("test-secret"), True),
        (httpx2.Response(500, text="test-secret"), True),
    ],
)
async def test_edit_put_errors_are_safe_and_not_retried(mock_jira, edit_document, response, uncertain):
    """Report permissions or uncertain writes without leaking upstream errors or retrying."""
    # Arrange
    requests, responses = mock_jira
    responses.extend([httpx2.Response(200, json={"body": edit_document}), response])

    # Act
    result = await invoke("edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", "body": "new"})

    # Assert
    assert result.is_error
    message = result.content[0].text
    assert "test-secret" not in message
    assert [request.method for request in requests] == ["GET", "PUT"]
    if uncertain:
        assert "get_issue_comments" in message
    else:
        assert "Edit" in message
        assert "comment" in message


async def test_edit_get_failure_does_not_warn_about_unknown_write(mock_jira):
    """A failed read does not imply that an edit might have been sent."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(500, text="test-secret"))

    # Act
    result = await invoke("edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", "body": "new"})

    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "get_issue_comments" not in result.content[0].text
    assert [request.method for request in requests] == ["GET"]


async def test_edit_rejects_expansion_before_replacement(mock_jira):
    """Reject projected oversized replacements before allocating the expanded text."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"body": markdown_to_adf("aaaa")}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {"issue": "DEMO-1", "comment_id": "42", "old_text": "a", "new_text": "b" * 32_767, "replace_all": True},
    )

    # Assert
    assert result.is_error
    assert "Replacement would exceed" in result.content[0].text
    assert [request.method for request in requests] == ["GET"]


async def test_edit_preview_diff_separates_unterminated_lines(mock_jira):
    """Show removed and added single-paragraph text on separate diff lines."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"body": markdown_to_adf("old")}))

    # Act
    result = await invoke(
        "edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", "body": "new", "validate_only": True}
    )

    # Assert
    assert not result.is_error
    assert "-old\n+new\n" in result.structured_content["diff"]
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize("preview", [True, False])
async def test_edit_raw_adf_preserves_native_diff_and_rich_content(mock_jira, edit_document, preview):
    """Submit native diff text and preserve all other ADF values in preview and write."""
    # Arrange
    requests, responses = mock_jira
    original = copy.deepcopy(edit_document)
    original["content"].append(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": line, "marks": [{"type": "code"}]}]}
                    ],
                }
                for line in ["- old", "+  indented"]
            ],
        }
    )
    original["unknown"] = {"nullable": None, "values": [True, 3, "unchanged"]}
    edited = copy.deepcopy(original)
    edited["content"][1] = {
        "type": "codeBlock",
        "attrs": {"language": "diff"},
        "content": [{"type": "text", "text": "- old\n+  indented\n"}],
    }
    responses.extend([httpx2.Response(200, json={"body": original}), httpx2.Response(204)])

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "body_adf": edited,
            "expected_hash": document_hash(original),
            "validate_only": preview,
        },
    )

    # Assert
    assert not result.is_error
    assert [request.method for request in requests] == (["GET"] if preview else ["GET", "PUT"])
    assert all(request.url.path == "/rest/api/3/issue/DEMO-1/comment/42" for request in requests)
    if preview:
        assert result.structured_content["adf"] == edited
        assert result.structured_content["before_hash"] == document_hash(original)
        assert result.structured_content["after_hash"] == document_hash(edited)
        assert isinstance(result.structured_content["diff"], str)
        assert isinstance(result.structured_content["diff_truncated"], bool)
        assert result.structured_content["validate_only"] is True
    else:
        assert json.loads(requests[1].content) == {"body": edited}
        assert result.structured_content == {
            "issue": "DEMO-1",
            "comment_id": "42",
            "updated": True,
            "body_hash": document_hash(edited),
        }


async def test_edit_raw_adf_stale_hash_is_get_only(mock_jira, edit_document):
    """Reject a stale source hash before submitting a raw replacement."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"body": edit_document}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "body_adf": edit_document,
            "expected_hash": "0" * 64,
        },
    )

    # Assert
    assert result.is_error
    assert "hash precondition" in result.content[0].text
    assert [request.method for request in requests] == ["GET"]


async def test_edit_raw_adf_structural_change_can_have_empty_diff(mock_jira, edit_document):
    """Expose changed structure and hashes even when rendered text is identical."""
    # Arrange
    requests, responses = mock_jira
    edited = copy.deepcopy(edit_document)
    edited["extra"]["new"] = None
    responses.append(httpx2.Response(200, json={"body": edit_document}))

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "body_adf": edited,
            "validate_only": True,
        },
    )

    # Assert
    assert not result.is_error
    assert result.structured_content["adf"] == edited
    assert result.structured_content["diff"] == ""
    assert result.structured_content["before_hash"] == document_hash(edit_document)
    assert result.structured_content["after_hash"] == document_hash(edited)
    assert document_hash(edit_document) != document_hash(edited)
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "args",
    [
        {"body_adf": {}},
        {"body_adf": {"type": "paragraph", "version": 1, "content": []}},
        {"body_adf": {"type": "doc", "version": 2, "content": []}},
        {"body_adf": {"type": "doc", "version": 1, "content": {}}},
        {"body_adf": {"type": "doc", "version": 1, "content": [], "extra": "x" * 100_000}},
        {"body_adf": {"type": "doc", "version": 1, "content": [], "extra": "é" * 50_000}},
        {"body_adf": {"type": "doc", "version": 1, "content": []}, "body": "new"},
        {"body_adf": {"type": "doc", "version": 1, "content": []}, "old_text": "old"},
        {"body_adf": {"type": "doc", "version": 1, "content": []}, "new_text": "new"},
        {"body_adf": {"type": "doc", "version": 1, "content": []}, "replace_all": True},
    ],
)
async def test_edit_raw_adf_rejects_invalid_input_before_get(mock_jira, args):
    """Reject invalid envelopes, byte sizes, and mixed modes without any HTTP request."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke("edit_issue_comment", {"issue": "DEMO-1", "comment_id": "42", **args})

    # Assert
    assert result.is_error
    assert "comment ADF" in result.content[0].text or "mutually exclusive" in result.content[0].text
    assert requests == []


async def test_edit_raw_adf_uncertain_write_is_not_retried(mock_jira, edit_document):
    """Send one PUT and give safe recovery guidance when its outcome is uncertain."""
    # Arrange
    requests, responses = mock_jira
    responses.extend([httpx2.Response(200, json={"body": edit_document}), httpx2.ReadTimeout("test-secret")])

    # Act
    result = await invoke(
        "edit_issue_comment",
        {
            "issue": "DEMO-1",
            "comment_id": "42",
            "body_adf": edit_document,
        },
    )

    # Assert
    assert result.is_error
    assert "get_issue_comments" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert [request.method for request in requests] == ["GET", "PUT"]
    assert json.loads(requests[1].content) == {"body": edit_document}
