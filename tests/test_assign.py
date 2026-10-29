"""Exercise the assignment tool through the MCP and Jira boundaries 👤."""

import json

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


def current(assignee):
    """Build Jira's response for an issue read limited to the assignee field."""
    value = None if assignee is None else {"accountId": assignee, "displayName": "Someone"}
    return httpx2.Response(200, json={"id": "10001", "key": "DEMO-1", "fields": {"assignee": value}})


@pytest.mark.parametrize(
    ("previous", "args"),
    [
        (None, {"account_id": "me"}),
        ("them", {"account_id": None}),
        (None, {"account_id": "me", "expected_assignee": None}),
        ("them", {"account_id": "me", "expected_assignee": "them"}),
    ],
)
async def test_assign_reads_then_puts_once(mock_jira, previous, args):
    """Read the current assignee, check the optional guard, and send exactly one PUT."""
    # Arrange
    requests, responses = mock_jira
    responses.extend([current(previous), httpx2.Response(204)])

    # Act
    result = await invoke("assign_issue", {"issue": "10001", **args})

    # Assert
    assert not result.is_error
    assert result.structured_content == {
        "issue": "DEMO-1",
        "assignee": args["account_id"],
        "previous_assignee": previous,
    }
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/rest/api/3/issue/10001"),
        ("PUT", "/rest/api/3/issue/10001/assignee"),
    ]
    assert dict(requests[0].url.params) == {"fields": "assignee"}
    assert json.loads(requests[1].content) == {"accountId": args["account_id"]}


@pytest.mark.parametrize(("previous", "expected"), [("them", None), (None, "them"), ("other", "them")])
async def test_guard_mismatch_makes_no_write(mock_jira, previous, expected):
    """Refuse without a PUT when the current assignee differs from expected_assignee."""
    # Arrange
    requests, responses = mock_jira
    responses.append(current(previous))

    # Act
    result = await invoke("assign_issue", {"issue": "DEMO-1", "account_id": "me", "expected_assignee": expected})

    # Assert
    assert result.is_error
    assert result.content[0].text.startswith("No change was made")
    assert f"assigned to {previous or 'nobody'}, not {expected or 'nobody'}" in result.content[0].text
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(400, text="test-secret"), "not be assignable"),
        (httpx2.Response(403, text="test-secret"), "Assign issues permission"),
        (httpx2.Response(404, text="test-secret"), "No change was made"),
        (httpx2.Response(500, text="test-secret"), "outcome may be unknown"),
        (httpx2.ReadTimeout("test-secret"), "outcome may be unknown"),
    ],
)
async def test_assign_errors_are_safe_and_not_retried(mock_jira, response, message):
    """Explain rejected and uncertain writes without upstream details or a retry."""
    # Arrange
    requests, responses = mock_jira
    responses.extend([current(None), response])

    # Act
    result = await invoke("assign_issue", {"issue": "DEMO-1", "account_id": "me"})

    # Assert
    assert result.is_error
    assert message in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert [request.method for request in requests] == ["GET", "PUT"]


async def test_hidden_assignee_field_fails_closed(mock_jira):
    """Do not treat a missing assignee field as unassigned, so the guard cannot pass by mistake."""
    # Arrange
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"id": "10001", "key": "DEMO-1", "fields": {}}))

    # Act
    result = await invoke("assign_issue", {"issue": "DEMO-1", "account_id": "me", "expected_assignee": None})

    # Assert
    assert result.is_error
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "args",
    [
        {"issue": "DEMO-1"},
        {"issue": "../bad", "account_id": "me"},
        {"issue": "DEMO-1", "account_id": " "},
        {"issue": "DEMO-1", "account_id": "me", "expected_assignee": ""},
    ],
)
async def test_invalid_assign_arguments_do_not_request(mock_jira, args):
    """Require account_id (null is allowed) and reject bad keys or IDs before any request."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke("assign_issue", args)

    # Assert
    assert result.is_error
    assert requests == []
