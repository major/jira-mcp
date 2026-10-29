"""Exercise field discovery and the field catalog cache through the MCP and Jira boundaries 🏷️."""

import asyncio

import httpx2
import pytest
from fastmcp import Client

from jira_mcp import mcp
from jira_mcp.client import JiraClient
from jira_mcp.fields import FieldCatalogCache
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


async def test_list_fields_filters_and_pages(mock_jira):
    """Find custom fields by name and retain stable field IDs."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json=[
                {"id": "customfield_2", "name": "Team B", "custom": True, "schema": {"type": "string"}},
                {"id": "summary", "name": "Summary", "custom": False},
                {"id": "customfield_1", "name": "Team A", "custom": True},
            ],
        )
    )

    result = await invoke("list_fields", {"search": "TEAM", "max_results": 1})

    assert result.structured_content == {
        "fields": [{"id": "customfield_1", "name": "Team A", "custom": True}],
        "start_at": 0,
        "total": 2,
        "next_start_at": 1,
    }
    assert requests[0].url.path == "/rest/api/3/field"


async def test_catalog_cache_reuses_data_for_search_and_pages(mock_jira):
    """Share one validated catalog across independent filters and pages."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json=[
                {"id": "customfield_2", "name": "Team", "custom": True},
                {"id": "summary", "name": "Summary"},
                {"id": "customfield_1", "name": "Team", "custom": True},
            ],
        )
    )

    async with Client(mcp) as client:
        first = await client.call_tool("list_fields", {"search": "TEAM", "max_results": 1})
        second = await client.call_tool("list_fields", {"search": "team", "start_at": 1, "max_results": 1})
        third = await client.call_tool("list_fields", {"search": "SUMMARY"})

    assert first.structured_content == {
        "fields": [{"id": "customfield_1", "name": "Team", "custom": True}],
        "start_at": 0,
        "total": 2,
        "next_start_at": 1,
    }
    assert second.structured_content["fields"] == [{"id": "customfield_2", "name": "Team", "custom": True}]
    assert second.structured_content["next_start_at"] is None
    assert third.structured_content["fields"] == [{"id": "summary", "name": "Summary", "custom": False}]
    assert len(requests) == 1


async def test_empty_catalog_is_cached(mock_jira):
    """A validated empty catalog is a cache hit, not a cache miss."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=[]))

    async with Client(mcp) as client:
        first = await client.call_tool("list_fields", {})
        second = await client.call_tool("list_fields", {"search": "missing"})

    assert first.structured_content == {"fields": [], "start_at": 0, "total": 0, "next_start_at": None}
    assert second.structured_content == first.structured_content
    assert len(requests) == 1


async def test_catalog_ttl_boundary_and_refresh(mock_jira, monkeypatch):
    """Expire at exactly five minutes and force a new catalog on refresh."""
    requests, responses = mock_jira
    clock = [100.0]
    monkeypatch.setattr("jira_mcp.fields.monotonic", lambda: clock[0])
    responses.extend(
        httpx2.Response(200, json=[{"id": "summary", "name": name}]) for name in ("First", "Second", "Third")
    )

    async with Client(mcp) as client:
        first = await client.call_tool("list_fields", {})
        clock[0] = 399.999
        before_expiry = await client.call_tool("list_fields", {})
        clock[0] = 400.0
        at_expiry = await client.call_tool("list_fields", {})
        refreshed = await client.call_tool("list_fields", {"refresh": True})
        reused = await client.call_tool("list_fields", {})

    assert first.structured_content["fields"][0]["name"] == "First"
    assert before_expiry.structured_content == first.structured_content
    assert at_expiry.structured_content["fields"][0]["name"] == "Second"
    assert refreshed.structured_content["fields"][0]["name"] == "Third"
    assert reused.structured_content == refreshed.structured_content
    expected_requests = 3
    assert len(requests) == expected_requests


@pytest.mark.parametrize(
    "bad_response",
    [httpx2.Response(500, text="test-secret"), httpx2.Response(200, json=[{"id": "test-secret"}])],
)
@pytest.mark.parametrize("state", ["empty", "expired", "refresh"])
async def test_catalog_failures_are_not_cached(mock_jira, monkeypatch, bad_response, state):
    """Retry failed catalog fetches instead of caching transport or validation errors."""
    requests, responses = mock_jira
    clock = [100.0]
    monkeypatch.setattr("jira_mcp.fields.monotonic", lambda: clock[0])
    if state != "empty":
        responses.append(httpx2.Response(200, json=[{"id": "summary", "name": "Old"}]))
    responses.extend([bad_response, httpx2.Response(200, json=[{"id": "summary", "name": "New"}])])

    async with Client(mcp) as client:
        if state != "empty":
            await client.call_tool("list_fields", {})
        if state == "expired":
            clock[0] += 300
        args = {"refresh": True} if state == "refresh" else {}
        failed = await client.call_tool("list_fields", args, raise_on_error=False)
        if state == "refresh":
            still_valid = await client.call_tool("list_fields", {})
            assert still_valid.structured_content["fields"][0]["name"] == "Old"
        retried = await client.call_tool("list_fields", args)

    assert failed.is_error
    assert "test-secret" not in failed.content[0].text
    assert retried.structured_content["fields"][0]["name"] == "New"
    assert len(requests) == (2 if state == "empty" else 3)


async def test_catalog_isolated_per_lifespan(mock_jira):
    """A new client lifespan fetches its own catalog rather than reusing old data."""
    requests, responses = mock_jira
    responses.extend(httpx2.Response(200, json=[{"id": "summary", "name": name}]) for name in ("First", "Second"))

    first = await invoke("list_fields")
    second = await invoke("list_fields")

    assert first.structured_content["fields"][0]["name"] == "First"
    assert second.structured_content["fields"][0]["name"] == "Second"
    expected_requests = 2
    assert len(requests) == expected_requests


@pytest.mark.parametrize("mode", ["cold", "expired", "refresh"])
async def test_concurrent_catalog_fetches_coalesce(monkeypatch, mode):
    """Concurrent misses and refreshes share the successful in-flight fetch."""
    started = asyncio.Event()
    second_entered = asyncio.Event()
    release = asyncio.Event()
    requests = []
    clock = [100.0]
    original_init = JiraClient.__init__
    original_get = FieldCatalogCache.get
    calls = 0

    async def handle(request):
        """Hold the concurrent fetch until both calls have entered the cache."""
        requests.append(request)
        if mode == "cold" or len(requests) > 1:
            started.set()
            await release.wait()
        return httpx2.Response(200, json=[{"id": "summary", "name": str(len(requests))}])

    def initialize(self, settings):
        """Install the controllable network-free Jira transport."""
        original_init(self, settings, transport=httpx2.MockTransport(handle))

    async def observed_get(self, refresh=False):
        """Signal after the second concurrent caller reaches the cache."""
        nonlocal calls
        calls += 1
        if calls == (2 if mode == "cold" else 3):
            second_entered.set()
        return await original_get(self, refresh=refresh)

    monkeypatch.setattr(JiraClient, "__init__", initialize)
    monkeypatch.setattr(FieldCatalogCache, "get", observed_get)
    monkeypatch.setattr("jira_mcp.fields.monotonic", lambda: clock[0])

    async with asyncio.timeout(5), Client(mcp) as client:
        if mode != "cold":
            await client.call_tool("list_fields", {})
        if mode == "expired":
            clock[0] += 300
        args = {"refresh": mode == "refresh"}
        first = asyncio.create_task(client.call_tool("list_fields", args))
        await started.wait()
        second = asyncio.create_task(client.call_tool("list_fields", args))
        await second_entered.wait()
        release.set()
        results = await asyncio.gather(first, second)

    assert results[0].structured_content == results[1].structured_content
    assert len(requests) == (1 if mode == "cold" else 2)


async def test_issue_field_discovery_fallbacks_values_and_freshness(mock_jira):
    """Describe only returned IDs, treating only null as lacking a value."""
    requests, responses = mock_jira
    payload = {
        "id": "1",
        "key": "DEMO-1",
        "fields": {
            "customfield_2": None,
            "customfield_1": {"private": "test-secret"},
            "empty_string": "",
            "empty_list": [],
            "empty_object": {},
            "false_value": False,
            "zero": 0,
        },
        "names": {"customfield_1": "Team", "customfield_2": "Team", "extra": "Not returned"},
        "schema": {"customfield_1": {"type": "option"}, "extra": {"type": "string"}},
    }
    updated = {**payload, "fields": {**payload["fields"], "customfield_2": "now set"}}
    responses.extend([httpx2.Response(200, json=payload), httpx2.Response(200, json=updated)])

    async with Client(mcp) as client:
        first = await client.call_tool("list_fields", {"issue": "DEMO-1", "refresh": True})
        second = await client.call_tool(
            "list_fields", {"issue": "DEMO-1", "search": "TEAM", "start_at": 1, "max_results": 1}
        )

    assert first.structured_content == {
        "fields": [
            {"id": "customfield_1", "name": "Team", "custom": True, "schema": {"type": "option"}, "has_value": True},
            {"id": "customfield_2", "name": "Team", "custom": True, "has_value": False},
            {"id": "empty_list", "name": "empty_list", "custom": False, "has_value": True},
            {"id": "empty_object", "name": "empty_object", "custom": False, "has_value": True},
            {"id": "empty_string", "name": "empty_string", "custom": False, "has_value": True},
            {"id": "false_value", "name": "false_value", "custom": False, "has_value": True},
            {"id": "zero", "name": "zero", "custom": False, "has_value": True},
        ],
        "start_at": 0,
        "total": 7,
        "next_start_at": None,
    }
    assert second.structured_content == {
        "fields": [{"id": "customfield_2", "name": "Team", "custom": True, "has_value": True}],
        "start_at": 1,
        "total": 2,
        "next_start_at": None,
    }
    assert "test-secret" not in str(first.structured_content)
    expected_requests = 2
    assert len(requests) == expected_requests
    assert all(request.url.path == "/rest/api/3/issue/DEMO-1" for request in requests)
    assert all(dict(request.url.params) == {"fields": "*all", "expand": "names,schema"} for request in requests)


async def test_issue_discovery_refresh_does_not_invalidate_catalog(mock_jira):
    """Keep the global cache untouched when issue discovery receives refresh=true."""
    requests, responses = mock_jira
    responses.extend(
        [
            httpx2.Response(200, json=[{"id": "summary", "name": "Summary"}]),
            httpx2.Response(200, json={"id": "1", "key": "DEMO-1", "fields": {"summary": None}}),
        ]
    )

    async with Client(mcp) as client:
        catalog = await client.call_tool("list_fields", {})
        issue = await client.call_tool("list_fields", {"issue": "DEMO-1", "refresh": True})
        reused = await client.call_tool("list_fields", {})

    assert catalog.structured_content == reused.structured_content
    assert issue.structured_content["fields"][0]["has_value"] is False
    assert [request.url.path for request in requests] == ["/rest/api/3/field", "/rest/api/3/issue/DEMO-1"]


@pytest.mark.parametrize("response", [httpx2.Response(403, text="test-secret"), httpx2.ReadTimeout("test-secret")])
async def test_issue_field_discovery_http_errors_are_safe(mock_jira, response):
    """Sanitize upstream and transport errors in issue-scoped discovery."""
    _, responses = mock_jira
    responses.append(response)

    result = await invoke("list_fields", {"issue": "DEMO-1"})

    assert result.is_error
    assert "test-secret" not in result.content[0].text


async def test_issue_discovery_id_search_and_page(mock_jira):
    """Apply case-insensitive ID search, sorting, and local pagination to issue fields."""
    _, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={"id": "1", "key": "DEMO-1", "fields": {"customfield_2": 2, "customfield_1": 1}},
        )
    )

    result = await invoke("list_fields", {"issue": "1", "search": "CUSTOMFIELD", "start_at": 1, "max_results": 1})

    assert result.structured_content == {
        "fields": [{"id": "customfield_2", "name": "customfield_2", "custom": True, "has_value": True}],
        "start_at": 1,
        "total": 2,
        "next_start_at": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "1", "key": "DEMO-1", "fields": []},
        {"id": "1", "key": "DEMO-1", "fields": {"summary": 1}, "names": {"summary": ["test-secret"]}},
        {"id": "1", "key": "DEMO-1", "fields": {"summary": 1}, "schema": ["test-secret"]},
        {"id": "1", "key": "DEMO-1", "fields": {"summary": 1}, "schema": {"summary": "test-secret"}},
    ],
)
async def test_issue_field_discovery_rejects_malformed_shapes(mock_jira, payload):
    """Reject malformed issue metadata without exposing response values."""
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke("list_fields", {"issue": "DEMO-1"})

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text


async def test_field_catalog_without_search(mock_jira):
    """Return IDs and schemas without requiring a search term."""
    _, responses = mock_jira
    fields = [{"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}}]
    responses.append(httpx2.Response(200, json=fields))

    result = await invoke("list_fields")

    assert result.structured_content["fields"] == fields
    assert result.structured_content["next_start_at"] is None
