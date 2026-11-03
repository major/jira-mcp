"""Exercise saved filters through the MCP and Jira boundaries."""

# ruff: noqa: PLR2004

import json

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")

NAME = "My filter"
JQL = "project = DEMO"
ARGS = {"name": NAME, "jql": JQL}
PAYLOAD = {**ARGS, "sharePermissions": [], "editPermissions": []}
CREATED = {
    "id": "42",
    **ARGS,
    "viewUrl": "https://example.atlassian.net/issues/?filter=42",
    "sharePermissions": [],
    "editPermissions": [],
    "self": "unneeded",
    "owner": {"accountId": "me"},
}


def preflight(responses, *, scope="PRIVATE", page=None):
    """Arrange successful read-only checks and the private creation prechecks."""
    responses.extend(
        [
            httpx2.Response(200, json={"queries": [{"query": JQL, "structure": {}}]}),
            httpx2.Response(200, json={"issues": [], "isLast": True}),
            httpx2.Response(200, json={"scope": scope}),
            httpx2.Response(200, json={"accountId": "me"}),
            httpx2.Response(
                200,
                json=page
                if page is not None
                else {"startAt": 0, "maxResults": 100, "total": 0, "isLast": True, "values": []},
            ),
        ]
    )


def assert_jql_requests(requests):
    """Check strict validation and the read-only one-result search."""
    parse, search = requests[:2]
    assert parse.method == "POST"
    assert parse.url.path == "/rest/api/3/jql/parse"
    assert dict(parse.url.params) == {"validation": "strict"}
    assert json.loads(parse.content) == {"queries": [JQL]}
    assert search.method == "POST"
    assert search.url.path == "/rest/api/3/search/jql"
    assert json.loads(search.content) == {"jql": JQL, "maxResults": 1, "fields": ["key"]}


async def test_create_private_filter_once(mock_jira):
    """Check preconditions and create exactly one explicitly private filter."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    responses.append(httpx2.Response(200, json=CREATED))

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert not result.is_error
    assert_jql_requests(requests)
    assert [r.url.path for r in requests[2:]] == [
        "/rest/api/3/filter/defaultShareScope",
        "/rest/api/3/myself",
        "/rest/api/3/filter/search",
        "/rest/api/3/filter",
    ]
    assert dict(requests[4].url.params) == {
        "filterName": NAME,
        "accountId": "me",
        "expand": "jql,viewUrl",
        "startAt": "0",
        "maxResults": "100",
    }
    assert requests[5].method == "POST"
    assert json.loads(requests[5].content) == PAYLOAD
    for key in ("id", "name", "jql", "viewUrl", "sharePermissions"):
        assert result.structured_content[key] == CREATED[key]
    assert "self" not in result.structured_content
    assert "owner" not in result.structured_content


async def test_create_preview_only_runs_read_only_checks(mock_jira):
    """Preview the private payload without creating or checking account defaults."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)

    # Act
    result = await invoke("create_filter", {**ARGS, "description": "A description", "validate_only": True})

    # Assert
    assert not result.is_error
    assert result.structured_content["validate_only"] is True
    assert result.structured_content["payload"] == {**PAYLOAD, "description": "A description"}
    assert len(requests) == 2
    assert_jql_requests(requests)


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, json={"queries": [{"query": JQL, "errors": ["bad JQL"]}]}),
        httpx2.Response(400, text="test-secret"),
    ],
)
async def test_invalid_jql_fails_before_create(mock_jira, response):
    """Reject strict parser errors without leaking Jira details or making further requests."""
    # Arrange
    requests, responses = mock_jira
    responses.append(response)

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    assert result.content[0].text.startswith("No filter was created")
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    "page",
    [
        {"startAt": 0, "maxResults": 100, "total": 1, "isLast": True, "values": [CREATED]},
        {"startAt": 0, "maxResults": 100, "total": 101, "isLast": False, "values": []},
    ],
)
async def test_duplicate_or_incomplete_owned_search_fails_closed(mock_jira, page):
    """Do not create if an owned exact name exists or absence cannot be established."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses, page=page)

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    assert result.content[0].text.startswith("No filter was created")
    assert len(requests) == 5
    assert requests[-1].url.path == "/rest/api/3/filter/search"


async def test_shared_default_rejected(mock_jira):
    """Refuse creation when the account default scope is not private."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses, scope="GLOBAL")

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    assert result.content[0].text.startswith("No filter was created")
    assert len(requests) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"sharePermissions": [{"type": "global"}]},
        {"sharePermissions": None},
        {"editPermissions": [{"type": "global"}]},
    ],
)
async def test_created_filter_with_unverified_sharing_is_uncertain(mock_jira, changes):
    """Never claim that a created filter with unsafe or missing sharing was not created."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    created = {**CREATED, **changes}
    if created["sharePermissions"] is None:
        del created["sharePermissions"]
    responses.append(httpx2.Response(200, json=created))

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    text = result.content[0].text
    assert "No filter was created" not in text
    assert "search_filters" in text
    assert "creat" in text.lower()
    assert "priv" in text.lower() or "shar" in text.lower()
    assert len(requests) == 6


async def test_missing_edit_permissions_is_accepted(mock_jira):
    """Accept a private creation response that omits optional edit permissions."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    responses.append(httpx2.Response(200, json={k: v for k, v in CREATED.items() if k != "editPermissions"}))

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert not result.is_error
    assert result.structured_content["sharePermissions"] == []
    assert len(requests) == 6


@pytest.mark.parametrize("changes", [{"sharePermissions": None}, {"editPermissions": None}, {"name": None}])
async def test_created_identity_survives_invalid_privacy_or_response(mock_jira, changes):
    """Report the known ID without leaking malformed response content."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    responses.append(httpx2.Response(200, json={**CREATED, **changes}))

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    assert "42" in result.content[0].text
    assert "search_filters" in result.content[0].text
    assert "No filter was created" not in result.content[0].text
    assert len(requests) == 6


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
        httpx2.Response(200, json={"unexpected": "test-secret"}),
    ],
)
async def test_uncertain_create_is_not_retried(mock_jira, response):
    """Direct uncertain outcomes to search without exposing upstream details."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    responses.append(response)

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    text = result.content[0].text
    assert "search_filters" in text
    assert "test-secret" not in text
    assert "No filter was created" not in text
    assert len(requests) == 6


async def test_create_rejection_is_definitive(mock_jira):
    """Report an HTTP 400 creation rejection as definitive and safe."""
    # Arrange
    requests, responses = mock_jira
    preflight(responses)
    responses.append(httpx2.Response(400, text="test-secret"))

    # Act
    result = await invoke("create_filter", ARGS)

    # Assert
    assert result.is_error
    text = result.content[0].text
    assert text.startswith("No filter was created")
    assert any(word in text.lower() for word in ("name", "jql", "duplicate"))
    assert "test-secret" not in text
    assert len(requests) == 6


async def test_search_filters_compact_page(mock_jira):
    """Return compact named filters and Jira's final-page metadata."""
    # Arrange
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(200, json={"startAt": 0, "maxResults": 20, "total": 1, "isLast": True, "values": [CREATED]})
    )

    # Act
    result = await invoke("search_filters", {"name": NAME})

    # Assert
    assert not result.is_error
    content = result.structured_content
    assert content["start_at"] == 0
    assert content["max_results"] == 20
    assert content["total"] == 1
    assert content["next_start_at"] is None
    item = content["filters"][0]
    assert item["id"] == "42"
    assert item["name"] == NAME
    assert item["jql"] == JQL
    assert "self" not in item
    assert "owner" not in item
    assert requests[0].url.path == "/rest/api/3/filter/search"
    assert dict(requests[0].url.params) == {
        "filterName": NAME,
        "expand": "jql,viewUrl",
        "startAt": "0",
        "maxResults": "20",
    }


async def test_list_my_filters_preserves_upstream_paging(mock_jira):
    """Scope to the current owner and continue even after a short non-final page."""
    # Arrange
    requests, responses = mock_jira
    responses.extend(
        [
            httpx2.Response(200, json={"accountId": "me"}),
            httpx2.Response(200, json={"startAt": 5, "maxResults": 10, "isLast": False, "values": [CREATED]}),
        ]
    )

    # Act
    result = await invoke("list_my_filters", {"start_at": 5, "max_results": 20})

    # Assert
    assert not result.is_error
    assert result.structured_content["next_start_at"] == 15
    assert result.structured_content["filters"][0]["id"] == "42"
    assert requests[0].url.path == "/rest/api/3/myself"
    assert dict(requests[1].url.params) == {
        "accountId": "me",
        "expand": "jql,viewUrl",
        "startAt": "5",
        "maxResults": "20",
    }


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_filter", {"name": " ", "jql": JQL}),
        ("create_filter", {"name": NAME, "jql": " "}),
        ("search_filters", {"name": " "}),
        ("search_filters", {"name": NAME, "start_at": -1}),
        ("list_my_filters", {"max_results": 0}),
    ],
)
async def test_invalid_arguments_make_no_request(mock_jira, tool, args):
    """Reject invalid names, JQL, and page bounds before contacting Jira."""
    # Arrange
    requests, _ = mock_jira

    # Act
    result = await invoke(tool, args)

    # Assert
    assert result.is_error
    assert result.content[0].text
    assert requests == []
