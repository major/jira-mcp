"""Exercise transition and link tools through the MCP and Jira boundaries."""

import json

import httpx2
import pytest
from fastmcp import Client

from jira_mcp import mcp
from tests.test_server import invoke
from tests.test_server import jira_env as jira_env  # noqa: PLC0414 - Re-export shared pytest fixture.
from tests.test_server import mock_jira as mock_jira  # noqa: PLC0414 - Re-export shared pytest fixture.


async def test_transition_discovery_preserves_metadata_and_resolution(mock_jira):
    """Keep transition status and field metadata, including nested null values."""
    requests, responses = mock_jira
    transition = {
        "id": "31",
        "name": "Resolve",
        "to": {"id": "3", "name": "Done", "statusCategory": {"key": "done"}},
        "fields": {
            "resolution": {
                "required": True,
                "schema": {"type": "resolution", "system": "resolution"},
                "allowedValues": [{"id": "10000", "name": "Fixed"}],
                "defaultValue": None,
                "operations": ["set"],
            },
            "customfield_100": {"name": "Reason", "required": False, "allowedValues": [None, {"id": "5"}]},
        },
        "hasScreen": True,
        "isAvailable": True,
    }
    responses.append(httpx2.Response(200, json={"transitions": [transition]}))

    result = await invoke("get_issue_transitions", {"issue": "DEMO-1"})

    assert not result.is_error
    assert result.structured_content == {"transitions": [transition]}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1/transitions"
    assert dict(requests[0].url.params) == {"expand": "transitions.fields"}


@pytest.mark.parametrize(
    ("tool", "payload", "expected"),
    [
        ("get_issue_transitions", {"transitions": []}, {"transitions": []}),
        ("list_issue_link_types", {"issueLinkTypes": []}, {"issue_link_types": []}),
        ("get_issue_remote_links", [], {"remote_links": []}),
    ],
)
async def test_empty_discovery(mock_jira, tool, payload, expected):
    """Return empty discovery results without inventing entries."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))
    args = {} if tool == "list_issue_link_types" else {"issue": "123"}

    result = await invoke(tool, args)

    assert not result.is_error
    assert result.structured_content == expected
    assert len(requests) == 1


async def test_link_type_discovery_keeps_directions_without_self(mock_jira):
    """Expose link direction names and IDs without Jira REST URLs."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "issueLinkTypes": [
                    {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks", "self": "url"}
                ]
            },
        )
    )

    result = await invoke("list_issue_link_types")

    assert not result.is_error
    assert result.structured_content == {
        "issue_link_types": [{"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]
    }
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/issueLinkType"


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("get_issue_transitions", {}),
        ("get_issue_transitions", {"transitions": None}),
        ("get_issue_transitions", {"transitions": [{"name": "test-secret", "to": {}, "fields": {}}]}),
        ("get_issue_transitions", {"transitions": [{"id": "1", "name": "test-secret", "to": [], "fields": {}}]}),
        ("get_issue_transitions", {"transitions": [{"id": "1", "name": "test-secret", "to": {}, "fields": []}]}),
        ("list_issue_link_types", {}),
        ("list_issue_link_types", {"issueLinkTypes": [{"id": "1", "name": "test-secret", "inward": "in"}]}),
        ("get_issue_remote_links", {}),
        ("get_issue_remote_links", [{"id": 1, "object": {"url": "https://example.org"}}]),
        ("get_issue_remote_links", [{"id": True, "object": {"url": "url", "title": "test-secret"}}]),
        ("get_issue_remote_links", [{"id": 1, "object": {"url": "", "title": "test-secret"}}]),
        ("get_issue_remote_links", [{"id": 1, "object": {"url": "url", "title": 123}}]),
        ("get_issue_remote_links", [{"id": 1, "object": {"url": "url", "title": "test-secret", "status": []}}]),
        ("get_issue_transitions", {"transitions": [{"id": "1", "name": "A", "to": {}, "fields": {"a": []}}]}),
    ],
)
async def test_malformed_discovery_is_safe(mock_jira, tool, payload):
    """Reject malformed discovery without exposing the upstream payload."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))
    args = {} if tool == "list_issue_link_types" else {"issue": "DEMO-1"}

    result = await invoke(tool, args)

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (None, {"transition": {"id": "31"}}),
        ({}, {"transition": {"id": "31"}, "fields": {}}),
        (
            {"resolution": {"id": "10000"}, "customfield_1": None},
            {"transition": {"id": "31"}, "fields": {"resolution": {"id": "10000"}, "customfield_1": None}},
        ),
    ],
)
async def test_transition_posts_envelope_and_accepts_empty_204(mock_jira, fields, expected):
    """Send only supplied fields, preserving null, and acknowledge the transition."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(204))
    args = {"issue": "DEMO-1", "transition_id": "31"}
    if fields is not None:
        args["fields"] = fields

    result = await invoke("transition_issue", args)

    assert not result.is_error
    assert result.structured_content == {"issue": "DEMO-1", "transition_id": "31", "success": True}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1/transitions"
    assert json.loads(requests[0].content) == expected


@pytest.mark.parametrize(
    ("inward", "outward", "expected_inward", "expected_outward"),
    [
        ("123", "DEMO-2", {"id": "123"}, {"key": "DEMO-2"}),
        ("DEMO-1", "456", {"key": "DEMO-1"}, {"id": "456"}),
    ],
)
async def test_create_issue_link_preserves_direction_and_accepts_empty_201(
    mock_jira, inward, outward, expected_inward, expected_outward
):
    """Send numeric IDs and keys using Jira's directional link format."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(201))

    result = await invoke(
        "create_issue_link", {"link_type_id": "10000", "inward_issue": inward, "outward_issue": outward}
    )

    assert not result.is_error
    assert result.structured_content == {"success": True}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/api/3/issueLink"
    assert json.loads(requests[0].content) == {
        "type": {"id": "10000"},
        "inwardIssue": expected_inward,
        "outwardIssue": expected_outward,
    }


async def test_remote_link_discovery_preserves_metadata(mock_jira):
    """Keep original remote-link metadata and remove the top-level Jira URL."""
    requests, responses = mock_jira
    link = {
        "id": 10000,
        "self": "https://jira.invalid/rest/remote/10000",
        "globalId": "external=1",
        "application": {"type": "example", "name": "Example"},
        "relationship": "relates to",
        "object": {
            "url": "custom://example/1",
            "title": "Document",
            "summary": "Details",
            "icon": {"url16x16": "https://example.org/icon.png", "title": "Icon"},
            "status": {"resolved": False, "icon": {"title": "Open"}},
        },
    }
    responses.append(httpx2.Response(200, json=[link]))

    result = await invoke("get_issue_remote_links", {"issue": "DEMO-1"})

    assert not result.is_error
    assert result.structured_content == {"remote_links": [{key: value for key, value in link.items() if key != "self"}]}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1/remotelink"


async def test_remote_link_duplicate_exact_url_does_not_update_title(mock_jira):
    """Return an existing ID and avoid a POST even if the title differs."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(200, json=[{"id": "12", "object": {"url": "https://example.org/", "title": "Old"}}])
    )

    result = await invoke(
        "create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": "New"}
    )

    assert not result.is_error
    assert result.structured_content == {"created": False, "id": "12"}
    assert len(requests) == 1
    assert requests[0].method == "GET"


async def test_remote_link_discovery_allows_empty_existing_title(mock_jira):
    """Accept existing titles as strings even when empty, without normalizing URLs."""
    requests, responses = mock_jira
    link = {"id": "12", "object": {"url": "custom://example", "title": ""}}
    responses.append(httpx2.Response(200, json=[link]))

    result = await invoke("get_issue_remote_links", {"issue": "DEMO-1"})

    assert not result.is_error
    assert result.structured_content == {"remote_links": [link]}
    assert len(requests) == 1


async def test_remote_link_creation_preserves_url_and_returns_actual_id(mock_jira):
    """Compare URLs exactly, then send the original URL and title without globalId."""
    requests, responses = mock_jira
    url = "HTTPS://Example.Org:443/%7euser?x=1&x=2#Fragment"
    title = "  A title  "
    responses.extend(
        [
            httpx2.Response(200, json=[{"id": 9, "object": {"url": "https://example.org/~user", "title": "Old"}}]),
            httpx2.Response(201, json={"id": 10000, "self": "https://jira.invalid/rest/remote/10000"}),
        ]
    )

    result = await invoke("create_issue_remote_link", {"issue": "123", "url": url, "title": title})

    assert not result.is_error
    assert result.structured_content == {"created": True, "id": 10000}
    assert [request.method for request in requests] == ["GET", "POST"]
    assert all(request.url.path == "/rest/api/3/issue/123/remotelink" for request in requests)
    assert json.loads(requests[1].content) == {"object": {"url": url, "title": title}}


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
        httpx2.Response(200, text="test-secret"),
        httpx2.Response(200, json={}),
        httpx2.Response(200, json=[{"id": 1, "object": {"url": "https://example.org/", "title": "Existing"}}, {}]),
    ],
)
async def test_remote_preflight_fails_closed(mock_jira, response):
    """Reject every bad preflight, even after a matching item, without posting."""
    requests, responses = mock_jira
    responses.append(response)

    result = await invoke(
        "create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": "Title"}
    )

    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "outcome may be unknown" not in result.content[0].text
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(400, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
        httpx2.Response(201, text="test-secret"),
        httpx2.Response(201, json={"id": "10000", "self": "https://jira.invalid/link"}),
        httpx2.Response(201, json={"id": True, "self": "https://jira.invalid/link"}),
        httpx2.Response(201, json={"id": 10000}),
    ],
)
async def test_remote_write_errors_are_safe_and_not_retried(mock_jira, response):
    """Reject malformed or failed writes without leaking details or retrying."""
    requests, responses = mock_jira
    responses.extend([httpx2.Response(200, json=[]), response])

    result = await invoke(
        "create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": "Title"}
    )

    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "outcome may be unknown" in result.content[0].text
    assert "inspect" in result.content[0].text.lower()
    assert "retry" in result.content[0].text.lower()
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_issue_transitions", {"issue": "../bad"}),
        ("get_issue_remote_links", {"issue": "DEMO-0"}),
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "0"}),
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "01"}),
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "1" * 256}),
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "1", "fields": []}),
        ("create_issue_link", {"link_type_id": "x", "inward_issue": "1", "outward_issue": "DEMO-1"}),
        ("create_issue_link", {"link_type_id": "1", "inward_issue": "../bad", "outward_issue": "DEMO-1"}),
        ("create_issue_link", {"link_type_id": "1", "inward_issue": "1", "outward_issue": "0"}),
        *[
            ("create_issue_remote_link", {"issue": "DEMO-1", "url": url, "title": "Title"})
            for url in (
                "",
                "https://",
                "ftp://example.org/file",
                "//example.org/path",
                "https://user:pass@example.org/",
                "https://@example.org/",
                "https://example.org/\npath",
                "https://example.org/\x00path",
                "https://example.org:invalid/",
                "https://example.org/" + "x" * 4096,
            )
        ],
        ("create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": " \t\n"}),
        ("create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": "x" * 256}),
        ("create_issue_remote_link", {"issue": "../bad", "url": "https://example.org/", "title": "Title"}),
    ],
)
async def test_invalid_workflow_arguments_do_not_request(mock_jira, tool, args):
    """Reject invalid path, ID, URL, and title inputs before any Jira request."""
    requests, _ = mock_jira

    result = await invoke(tool, args)

    assert result.is_error
    assert requests == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "31"}),
        ("create_issue_link", {"link_type_id": "1", "inward_issue": "1", "outward_issue": "DEMO-1"}),
    ],
)
@pytest.mark.parametrize("response", [httpx2.Response(403, text="test-secret"), httpx2.ReadTimeout("test-secret")])
async def test_bodyless_write_errors_are_safe_and_not_retried(mock_jira, tool, args, response):
    """Do not leak upstream errors or retry ambiguous transition and link writes."""
    requests, responses = mock_jira
    responses.append(response)

    result = await invoke(tool, args)

    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert "outcome may be unknown" in result.content[0].text
    assert "inspect" in result.content[0].text.lower()
    assert "retry" in result.content[0].text.lower()
    assert len(requests) == 1
    assert requests[0].method == "POST"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("transition_issue", {"issue": "DEMO-1", "transition_id": "31"}),
        ("create_issue_link", {"link_type_id": "1", "inward_issue": "1", "outward_issue": "DEMO-1"}),
        ("create_issue_remote_link", {"issue": "DEMO-1", "url": "https://example.org/", "title": "Title"}),
    ],
)
async def test_missing_configuration_is_not_reported_as_an_attempted_write(monkeypatch, tool, args):
    """Keep configuration failures distinct from errors after a POST attempt."""
    monkeypatch.delenv("JIRA_API_TOKEN")

    result = await invoke(tool, args)

    assert result.is_error
    assert "JIRA_API_TOKEN" in result.content[0].text
    assert "outcome may be unknown" not in result.content[0].text


async def test_workflow_tool_annotations():
    """Describe discovery as read-only and writes as potentially destructive."""
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("get_issue_transitions", "list_issue_link_types", "get_issue_remote_links"):
        annotation = tools[name].annotations
        assert annotation.read_only_hint is True
        assert annotation.destructive_hint is False
        assert annotation.idempotent_hint is True
        assert annotation.open_world_hint is True
    for name in ("transition_issue", "create_issue_link", "create_issue_remote_link"):
        annotation = tools[name].annotations
        assert annotation.read_only_hint is False
        assert annotation.destructive_hint is True
        assert annotation.idempotent_hint is False
        assert annotation.open_world_hint is True
    description = tools["create_issue_link"].description
    assert description is not None
    assert "inward_issue blocks outward_issue" in description
