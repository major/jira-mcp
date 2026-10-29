"""Exercise the comment write tool through the MCP and Jira boundaries 💬."""

import json

import httpx2
import pytest

from jira_mcp.text import markdown_to_adf
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")

BODY = "Fixed in `main`.\n\n- added tests"
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
