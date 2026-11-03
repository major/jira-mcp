"""Exercise board and saved filter reads through the MCP and Jira boundaries."""

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")

BOARD = {"id": 7, "name": "Team", "type": "scrum", "self": "https://evil.invalid/board"}
FILTER = {
    "id": "42",
    "name": "Team issues",
    "jql": "project = TEAM ORDER BY created DESC",
    "viewUrl": "https://example.atlassian.net/issues/?filter=42",
    "owner": {"displayName": "A Person", "avatarUrls": {"x": "secret"}},
    "self": "https://evil.invalid/filter",
    "sharePermissions": [{"secret": "hidden"}],
}
CONFIG = {
    "filter": {"id": "42", "self": "https://evil.invalid/filter"},
    "columnConfig": {
        "columns": [
            {"name": "Doing", "statuses": [{"id": "1"}, {"id": "2"}, {"id": "999"}]},
        ]
    },
}
STATUSES = [{"id": "1", "name": "Open"}, {"id": "2", "name": "Review"}]
PATHS = [
    "/rest/agile/1.0/board/7",
    "/rest/agile/1.0/board/7/configuration",
    "/rest/api/3/filter/42",
    "/rest/api/3/status",
]
FILTER_INDEX = 2


def assert_requests(requests, paths):
    """Check the exact request sequence, method, and trusted origin."""
    assert [request.url.path for request in requests] == paths
    assert all(
        request.method == "GET" and str(request.url).startswith("https://example.atlassian.net/")
        for request in requests
    )


def board_responses(config=CONFIG, board=BOARD):
    """Return the successful Jira responses in request order."""
    return [httpx2.Response(200, json=value) for value in (board, config, FILTER, STATUSES)]


async def test_scrum_board_maps_statuses_and_preserves_filter_jql(mock_jira):
    """Resolve board metadata, filter, and one catalog without dropping unknown IDs."""
    # Arrange
    requests, responses = mock_jira
    responses.extend(board_responses())

    # Act
    result = await invoke("get_board", {"board_id": 7})

    # Assert
    assert not result.is_error
    data = result.structured_content
    assert (data["id"], data["name"], data["type"]) == (7, "Team", "scrum")
    assert data["filter"] == {"id": "42", "name": "Team issues", "jql": FILTER["jql"]}
    assert data["columns"] == [
        {
            "name": "Doing",
            "statuses": [{"id": "1", "name": "Open"}, {"id": "2", "name": "Review"}, {"id": "999", "name": None}],
        }
    ]
    assert data["jql"] == "filter = 42"
    assert_requests(requests, PATHS)


async def test_kanban_board_keeps_empty_backlog_and_subquery(mock_jira):
    """Keep an empty backlog and combine board scope without rewriting saved JQL."""
    # Arrange
    requests, responses = mock_jira
    config = {
        "filter": {"id": "42"},
        "columnConfig": {"columns": [{"name": "Backlog", "statuses": []}]},
        "subQuery": {"query": "fixVersion in unreleasedVersions()"},
    }
    responses.extend(board_responses(config, {**BOARD, "type": "kanban"}))

    # Act
    result = await invoke("get_board", {"board_id": 7})

    # Assert
    assert not result.is_error
    data = result.structured_content
    assert data["type"] == "kanban"
    assert data["columns"] == [{"name": "Backlog", "statuses": []}]
    assert data["subquery"] == "fixVersion in unreleasedVersions()"
    assert data["jql"] == "filter = 42 AND (fixVersion in unreleasedVersions())"
    assert data["filter"]["jql"] == FILTER["jql"]
    assert_requests(requests, PATHS)


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("failed_index", [2, 3])
async def test_board_optional_read_access_failure_degrades_safely(mock_jira, status, failed_index):
    """Degrade only inaccessible filter or status reads, preserving board structure."""
    # Arrange
    requests, responses = mock_jira
    arranged = board_responses()
    arranged[failed_index] = httpx2.Response(status, text="test-secret")
    responses.extend(arranged)

    # Act
    result = await invoke("get_board", {"board_id": 7})

    # Assert
    assert not result.is_error
    data = result.structured_content
    assert "test-secret" not in str(data)
    if failed_index == FILTER_INDEX:
        assert data["filter"]["id"] == "42"
        assert data["filter"]["name"] is None
        assert data["filter"]["jql"] is None
        assert data["filter"]["unavailable_reason"]
        assert data["jql"] is None
        assert data["columns"][0]["statuses"][0]["name"] == "Open"
    else:
        assert data["status_names_unavailable"]
        assert data["columns"][0]["statuses"] == [
            {"id": "1", "name": None},
            {"id": "2", "name": None},
            {"id": "999", "name": None},
        ]
        assert data["jql"] == "filter = 42"
    assert_requests(requests, PATHS)


@pytest.mark.parametrize("failed_index", [2, 3])
@pytest.mark.parametrize("failure", [401, 429, 500, "malformed", "invalid_shape"])
async def test_board_other_optional_read_errors_propagate(mock_jira, failed_index, failure):
    """Do not hide authorization, rate limit, server, or malformed response errors."""
    # Arrange
    requests, responses = mock_jira
    arranged = board_responses()
    if failure == "invalid_shape":
        arranged[failed_index] = httpx2.Response(200, json={"unexpected": "test-secret"})
    else:
        arranged[failed_index] = httpx2.Response(200 if failure == "malformed" else failure, text="test-secret")
    responses.extend(arranged)

    # Act
    result = await invoke("get_board", {"board_id": 7})

    # Assert
    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert_requests(requests, PATHS[: failed_index + 1])


@pytest.mark.parametrize("failed_index", [0, 1])
@pytest.mark.parametrize("status", [403, 404])
async def test_board_required_read_access_failure_is_clear(mock_jira, failed_index, status):
    """Report an inaccessible board rather than returning a partial result."""
    # Arrange
    requests, responses = mock_jira
    arranged = board_responses()
    arranged[failed_index] = httpx2.Response(status, text="test-secret")
    responses.extend(arranged)

    # Act
    result = await invoke("get_board", {"board_id": 7})

    # Assert
    assert result.is_error
    message = result.content[0].text.lower()
    assert "board" in message
    assert "not found" in message or "inaccessible" in message
    assert "test-secret" not in message
    assert_requests(requests, PATHS[: failed_index + 1])


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("get_board", {"board_id": value}) for value in (0, -1, "../7", "https://evil.invalid")],
)
async def test_invalid_ids_never_request(mock_jira, tool, arguments):
    """Reject invalid IDs before making a Jira request."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke(tool, arguments)

    # Assert
    assert result.is_error
    assert requests == []
