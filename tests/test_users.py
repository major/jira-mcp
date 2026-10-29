"""Exercise user lookup tools through the MCP and Jira boundaries 👤."""

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


@pytest.mark.parametrize("email", [None, "reader@example.com"])
async def test_current_user_identity_and_privacy(mock_jira, email):
    """Identify the credential owner without requiring a visible email address."""
    requests, responses = mock_jira
    user = {"accountId": "opaque:123", "displayName": "Reader", "active": False, "accountType": "atlassian"}
    responses.append(httpx2.Response(200, json={**user, "emailAddress": email, "avatarUrls": {}, "self": "noise"}))

    result = await invoke("get_current_user")

    assert not result.is_error
    assert result.structured_content == {**user, **({"emailAddress": email} if email else {})}
    assert requests[0].url.path == "/rest/api/3/myself"
    assert not requests[0].url.params


async def test_user_search_preserves_ambiguous_matches(mock_jira):
    """Keep distinct account IDs and omit profile fields hidden by Jira privacy."""
    requests, responses = mock_jira
    users = [
        {"accountId": "first", "displayName": "Alex Reader", "active": True},
        {"accountId": "second", "displayName": "Alex Reader", "emailAddress": "alex@example.com"},
    ]
    responses.append(httpx2.Response(200, json=[{**user, "avatarUrls": {}} for user in users]))

    result = await invoke("search_users", {"query": "Alex Reader+team@example.com", "start_at": 5, "max_results": 10})

    assert not result.is_error
    assert result.structured_content == {"users": users, "start_at": 5, "next_start_at": 7, "limit_reached": False}
    assert requests[0].url.path == "/rest/api/3/user/search"
    assert dict(requests[0].url.params) == {"query": "Alex Reader+team@example.com", "startAt": "5", "maxResults": "10"}


@pytest.mark.parametrize(
    ("start", "count", "size", "expected"),
    [
        (0, 20, 20, (20, 20, False)),
        (0, 1, 20, (20, 1, False)),
        (0, 0, 20, (20, None, False)),
        (20, 0, 20, (20, None, False)),
        (999, 1, 100, (1, None, True)),
        (999, 0, 100, (1, None, False)),
        (990, 10, 20, (10, None, True)),
    ],
)
async def test_user_search_pagination(mock_jira, start, count, size, expected):
    """Continue nonempty pages and expose the first-1,000 matching-user boundary."""
    requests, responses = mock_jira
    effective_size, next_start, limit_reached = expected
    users = [{"accountId": str(index), "displayName": "Reader"} for index in range(count)]
    responses.append(httpx2.Response(200, json=users))

    result = await invoke("search_users", {"query": "Reader", "start_at": start, "max_results": size})

    assert not result.is_error
    assert result.structured_content == {
        "users": users,
        "start_at": start,
        "next_start_at": next_start,
        "limit_reached": limit_reached,
    }
    assert requests[0].url.params["maxResults"] == str(effective_size)


@pytest.mark.parametrize(
    ("name", "args", "payload"),
    [
        ("get_current_user", {}, []),
        ("get_current_user", {}, {"accountId": "test-secret"}),
        ("get_current_user", {}, {"accountId": "", "displayName": "test-secret"}),
        ("search_users", {"query": "Reader"}, {}),
        ("search_users", {"query": "Reader"}, ["test-secret"]),
        ("search_users", {"query": "Reader"}, [{"accountId": "test-secret", "displayName": ""}]),
    ],
)
async def test_invalid_user_shapes_are_safe(mock_jira, name, args, payload):
    """Validate identity fields without leaking malformed profile data in errors."""
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke(name, args)

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text
