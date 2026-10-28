"""Exercise the public MCP tools against Jira's HTTP boundary 🧪."""

import asyncio
import base64
import json

import httpx2
import pytest
from fastmcp import Client

from jira_mcp import main, mcp
from jira_mcp.client import JiraClient
from jira_mcp.fields import FieldCatalogCache


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    """Provide deterministic Cloud credentials without using a real token."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-secret")
    monkeypatch.delenv("JIRA_TIMEOUT", raising=False)


@pytest.fixture
def mock_jira(monkeypatch):
    """Inject a native HTTPX2 transport while preserving the real client lifecycle."""
    requests = []
    responses = []
    original_init = JiraClient.__init__

    def handle(request):
        """Capture one request and return the next arranged response."""
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def initialize(self, settings):
        """Use the production initializer with a network-free transport."""
        original_init(self, settings, transport=httpx2.MockTransport(handle))

    monkeypatch.setattr(JiraClient, "__init__", initialize)
    return requests, responses


async def invoke(name, arguments=None):
    """Call a registered tool through the MCP protocol."""
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments or {}, raise_on_error=False)


async def test_issue_defaults_and_cloud_auth(mock_jira):
    """Retrieve a compact ticket with text description and field names."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "id": "123",
                "key": "DEMO-1",
                "fields": {
                    "summary": "Hello",
                    "description": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Details"}]}],
                    },
                    "customfield_100": None,
                },
                "names": {"customfield_100": "Team", "unused": "Unused"},
                "schema": {"customfield_100": {"type": "string"}},
            },
        )
    )

    result = await invoke("get_issue", {"issue": "DEMO-1"})

    assert not result.is_error
    data = result.structured_content
    assert data["key"] == "DEMO-1"
    assert data["description"] == {"text": "Details", "characters": 7, "truncated": False}
    assert data["fields"] == {"summary": "Hello", "customfield_100": None}
    assert data["field_metadata"] == {"customfield_100": {"name": "Team", "schema": {"type": "string"}}}
    request = requests[0]
    assert str(request.url).startswith("https://example.atlassian.net/rest/api/3/issue/DEMO-1?")
    assert "description" in request.url.params["fields"]
    assert request.url.params["expand"] == "names,schema"
    encoded = base64.b64encode(b"reader@example.com:test-secret").decode()
    assert request.headers["authorization"] == f"Basic {encoded}"


@pytest.mark.parametrize("has_description", [True, False])
async def test_issue_null_and_absent_description(mock_jira, has_description):
    """Distinguish null from an omitted description and retain its metadata."""
    _, responses = mock_jira
    fields = {"summary": "Example", **({"description": None} if has_description else {})}
    responses.append(
        httpx2.Response(
            200,
            json={
                "id": "1",
                "key": "DEMO-1",
                "fields": fields,
                "names": {"description": "Description"},
                "schema": {"description": {"type": "any"}},
            },
        )
    )

    result = await invoke("get_issue", {"issue": "DEMO-1", "include_raw_adf": True})

    assert not result.is_error
    data = result.structured_content
    assert data["fields"] == {"summary": "Example"}
    if has_description:
        assert data["description"] == {"text": "", "characters": 0, "truncated": False, "adf": None}
        assert data["field_metadata"] == {"description": {"name": "Description", "schema": {"type": "any"}}}
    else:
        assert "description" not in data
        assert data["field_metadata"] == {}


async def test_issue_requires_fields(mock_jira):
    """Keep single-issue validation strict when Jira omits the fields object."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"id": "1", "key": "DEMO-1"}))

    result = await invoke("get_issue", {"issue": "DEMO-1"})

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize("fields", [["customfield_100"], ["*all"]])
async def test_explicit_fields_and_gateway(mock_jira, monkeypatch, fields):
    """Keep the scoped-token gateway path and explicit field selection."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_URL", "https://api.atlassian.com/ex/jira/cloud-id/")
    responses.append(
        httpx2.Response(
            200,
            json={
                "id": "1",
                "key": "MOVED-2",
                "fields": {"customfield_100": {"value": "A"}},
            },
        )
    )

    result = await invoke("get_issue", {"issue": "123", "fields": fields})

    assert not result.is_error
    assert result.structured_content["fields"]["customfield_100"] == {"value": "A"}
    assert "description" not in result.structured_content
    assert requests[0].url.path == "/ex/jira/cloud-id/rest/api/3/issue/123"
    assert requests[0].url.params["fields"] == ",".join(fields)


async def test_issue_values_and_duplicate_names_are_self_describing(mock_jira):
    """Return original nested custom values and matching metadata in one request."""
    requests, responses = mock_jira
    values = {
        "customfield_1": {"value": "one", "child": {"value": "nested"}},
        "customfield_2": [{"id": "two"}, {"id": "three"}],
        "customfield_3": None,
    }
    metadata = {
        "customfield_1": {"name": "Team", "schema": {"type": "option"}},
        "customfield_2": {"name": "Team", "schema": {"type": "array", "items": "option"}},
        "customfield_3": {"name": "Empty", "schema": {"type": "string"}},
    }
    responses.append(
        httpx2.Response(
            200,
            json={
                "id": "1",
                "key": "DEMO-1",
                "fields": values,
                "names": {key: item["name"] for key, item in metadata.items()},
                "schema": {key: item["schema"] for key, item in metadata.items()},
            },
        )
    )

    result = await invoke("get_issue", {"issue": "DEMO-1", "fields": ["*all"]})

    assert not result.is_error
    assert result.structured_content["fields"] == values
    assert result.structured_content["field_metadata"] == metadata
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/api/3/issue/DEMO-1"
    assert dict(requests[0].url.params) == {"fields": "*all", "expand": "names,schema"}


async def test_bulk_issues_defaults_deduplication_and_canonical_order(mock_jira):
    """Send one request with exact duplicates removed and retain Jira's response order."""
    requests, responses = mock_jira
    issues = ["old-2", "OLD-2", "2", "OLD-1", "HIDDEN-3", "old-2", "2"]
    returned = [
        {"id": "1", "key": "NEW-1", "fields": {"summary": "First"}},
        {"id": "2", "key": "NEW-2", "fields": {"summary": "Second"}},
        {"id": "2", "key": "NEW-2", "fields": {"summary": "Second"}},
    ]
    responses.append(httpx2.Response(200, json={"issues": returned, "issueErrors": []}))

    result = await invoke("get_issues", {"issues": issues})

    assert not result.is_error
    assert result.structured_content == {
        "issues": [{**issue, "field_metadata": {}} for issue in returned],
        "errors": [],
        "omissions_possible": True,
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/api/3/issue/bulkfetch"
    assert not request.url.params
    assert json.loads(request.content) == {
        "issueIdsOrKeys": ["old-2", "OLD-2", "2", "OLD-1", "HIDDEN-3"],
        "fields": [
            "summary",
            "status",
            "issuetype",
            "priority",
            "assignee",
            "reporter",
            "labels",
            "created",
            "updated",
            "description",
        ],
        "expand": ["names", "schema"],
    }
    encoded = base64.b64encode(b"reader@example.com:test-secret").decode()
    assert request.headers["authorization"] == f"Basic {encoded}"


@pytest.mark.parametrize("fields", [["customfield_100", "summary"], ["*all"]])
async def test_bulk_issues_explicit_fields_and_gateway(mock_jira, monkeypatch, fields):
    """Replace default fields unchanged when using the scoped-token Cloud gateway."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_URL", "https://api.atlassian.com/ex/jira/cloud-id/")
    responses.append(httpx2.Response(200, json={"issues": []}))

    result = await invoke("get_issues", {"issues": ["123"], "fields": fields})

    assert not result.is_error
    assert result.structured_content == {"issues": [], "errors": [], "omissions_possible": True}
    assert len(requests) == 1
    assert requests[0].url.path == "/ex/jira/cloud-id/rest/api/3/issue/bulkfetch"
    assert json.loads(requests[0].content) == {
        "issueIdsOrKeys": ["123"],
        "fields": fields,
        "expand": ["names", "schema"],
    }


@pytest.mark.parametrize(("count", "text_limit"), [(1, 1), (20, 100_000)])
async def test_bulk_issues_argument_bounds_are_inclusive(mock_jira, count, text_limit):
    """Accept one to twenty issues and both supported per-issue text limits."""
    requests, responses = mock_jira
    issues = [f"DEMO-{index}" for index in range(1, count + 1)]
    responses.append(httpx2.Response(200, json={"issues": []}))

    result = await invoke("get_issues", {"issues": issues, "text_limit": text_limit})

    assert not result.is_error
    assert json.loads(requests[0].content)["issueIdsOrKeys"] == issues
    assert len(requests) == 1


async def test_bulk_issues_accept_maximum_field_and_id_lengths(mock_jira):
    """Accept a 255-digit issue ID and the maximum number of selected fields."""
    requests, responses = mock_jira
    issue_id = "1" * 255
    fields = [f"customfield_{index}" for index in range(100)]
    responses.append(httpx2.Response(200, json={"issues": [], "issueErrors": [{"id": issue_id}]}))

    result = await invoke("get_issues", {"issues": [issue_id], "fields": fields})

    assert not result.is_error
    assert result.structured_content == {
        "issues": [],
        "errors": [{"id": issue_id, "message": "Jira could not return this issue."}],
        "omissions_possible": True,
    }
    assert json.loads(requests[0].content)["fields"] == fields
    assert json.loads(requests[0].content)["issueIdsOrKeys"] == [issue_id]
    assert len(requests) == 1


async def test_bulk_issues_keep_metadata_from_each_issue(mock_jira):
    """Use each issue's own names and schemas instead of sharing page metadata."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "issues": [
                    {
                        "id": "1",
                        "key": "DEMO-1",
                        "fields": {"customfield_1": "one"},
                        "names": {"customfield_1": "First"},
                        "schema": {"customfield_1": {"type": "string"}},
                    },
                    {
                        "id": "2",
                        "key": "DEMO-2",
                        "fields": {"customfield_1": 2},
                        "names": {"customfield_1": "Second"},
                        "schema": {"customfield_1": {"type": "number"}},
                    },
                ]
            },
        )
    )

    result = await invoke("get_issues", {"issues": ["DEMO-1", "DEMO-2"]})

    assert not result.is_error
    assert [issue["field_metadata"] for issue in result.structured_content["issues"]] == [
        {"customfield_1": {"name": "First", "schema": {"type": "string"}}},
        {"customfield_1": {"name": "Second", "schema": {"type": "number"}}},
    ]
    assert len(requests) == 1


@pytest.mark.parametrize("description", ["adf", "null", "absent"])
async def test_bulk_issues_render_like_single_issue(mock_jira, description):
    """Preserve each issue's metadata, nested values, and description behavior."""
    requests, responses = mock_jira
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "abcdef"}]}]}
    fields = {"customfield_1": {"child": [1, {"value": "nested"}]}, "customfield_2": None}
    if description != "absent":
        fields["description"] = adf if description == "adf" else None
    issue = {
        "id": "123",
        "key": "MOVED-2",
        "fields": fields,
        "names": {"description": "Description", "customfield_1": "Team", "customfield_2": "Team"},
        "schema": {"description": {"type": "any"}, "customfield_1": {"type": "option"}},
    }
    responses.extend([httpx2.Response(200, json={"issues": [issue]}), httpx2.Response(200, json=issue)])
    options = {"fields": ["*all"], "include_raw_adf": True, "text_limit": 3}

    bulk = await invoke("get_issues", {"issues": ["OLD-2"], **options})
    single = await invoke("get_issue", {"issue": "OLD-2", **options})

    assert not bulk.is_error
    assert not single.is_error
    item = bulk.structured_content["issues"][0]
    assert item == single.structured_content
    assert item["fields"] == {"customfield_1": {"child": [1, {"value": "nested"}]}, "customfield_2": None}
    assert item["field_metadata"] == {
        "customfield_1": {"name": "Team", "schema": {"type": "option"}},
        "customfield_2": {"name": "Team"},
        **({"description": {"name": "Description", "schema": {"type": "any"}}} if description != "absent" else {}),
    }
    if description == "adf":
        assert item["description"] == {"text": "abc", "characters": 6, "truncated": True, "adf": adf}
    elif description == "null":
        assert item["description"] == {"text": "", "characters": 0, "truncated": False, "adf": None}
    else:
        assert "description" not in item
    assert [request.url.path for request in requests] == ["/rest/api/3/issue/bulkfetch", "/rest/api/3/issue/OLD-2"]


async def test_bulk_issues_default_text_limit_applies_to_each_issue(mock_jira):
    """Apply the single-issue default per description without including raw ADF."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "issues": [
                    {"id": "1", "key": "DEMO-1", "fields": {"description": "x" * 12_001}},
                    {"id": "2", "key": "DEMO-2", "fields": {"description": "y" * 12_002}},
                ]
            },
        )
    )

    result = await invoke("get_issues", {"issues": ["1", "2"]})

    assert not result.is_error
    assert [issue["description"] for issue in result.structured_content["issues"]] == [
        {"text": "x" * 12_000, "characters": 12_001, "truncated": True},
        {"text": "y" * 12_000, "characters": 12_002, "truncated": True},
    ]
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("payload", "expected_issues", "expected_errors"),
    [
        (
            {"issues": [{"id": "1", "key": "DEMO-1", "fields": {}}], "issueErrors": [{"id": "2"}]},
            [{"id": "1", "key": "DEMO-1", "fields": {}, "field_metadata": {}}],
            [{"id": "2", "message": "Jira could not return this issue."}],
        ),
        (
            {"issues": [], "issueErrors": [{"id": "123", "errorMessage": "test-secret"}]},
            [],
            [{"id": "123", "message": "Jira could not return this issue."}],
        ),
        ({"issues": [], "issueErrors": []}, [], []),
        ({"issues": []}, [], []),
    ],
)
async def test_bulk_issues_partial_empty_and_error_results(mock_jira, payload, expected_issues, expected_errors):
    """Report only Jira's returned issues and safe errors, even when entries are omitted."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke("get_issues", {"issues": ["DEMO-1", "DEMO-2", "MISSING-3"]})

    assert not result.is_error
    assert result.structured_content == {
        "issues": expected_issues,
        "errors": expected_errors,
        "omissions_possible": True,
    }
    assert "test-secret" not in str(result.structured_content)
    assert len(requests) == 1


@pytest.mark.parametrize("error_message", [None, 42, ["test-secret"], {"private": "test-secret"}, "test-secret"])
async def test_bulk_issues_ignore_upstream_error_message(mock_jira, error_message):
    """Never validate or expose Jira's untrusted per-issue error message."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(200, json={"issues": [], "issueErrors": [{"id": "9", "errorMessage": error_message}]})
    )

    result = await invoke("get_issues", {"issues": ["9"]})

    assert not result.is_error
    assert result.structured_content == {
        "issues": [],
        "errors": [{"id": "9", "message": "Jira could not return this issue."}],
        "omissions_possible": True,
    }
    assert "test-secret" not in str(result)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"issues": None},
        {"issues": {}},
        {"issues": ["test-secret"]},
        {"issues": [{"id": "1", "key": "DEMO-1"}]},
        {"issues": [{"id": "1", "fields": {}}]},
        {"issues": [{"key": "DEMO-1", "fields": {}}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": None}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": []}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": {}, "names": {"a": "test-secret"}, "schema": []}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": {}, "names": {"a": ["test-secret"]}}]},
        {"issues": [], "issueErrors": None},
        {"issues": [], "issueErrors": {}},
        {"issues": [], "issueErrors": ["test-secret"]},
        {"issues": [], "issueErrors": [{}]},
        {"issues": [], "issueErrors": [{"id": "test-secret"}]},
        {"issues": [], "issueErrors": [{"id": "0"}]},
        {"issues": [], "issueErrors": [{"id": "01"}]},
        {"issues": [], "issueErrors": [{"id": "1" * 256}]},
        {"issues": [], "issueErrors": [{"id": 1}]},
        {"issues": [], "issueErrors": [{"id": True}]},
    ],
)
async def test_bulk_issues_invalid_payloads_are_safe(mock_jira, payload):
    """Reject malformed envelopes, issue details, and error IDs without leaking data."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke("get_issues", {"issues": ["DEMO-1"]})

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


async def test_issue_search_defaults_and_issue_shapes(mock_jira):
    """Render each issue like get_issue using the shared page metadata."""
    requests, responses = mock_jira
    body = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "abcdef"}]}]}
    responses.append(
        httpx2.Response(
            200,
            json={
                "issues": [
                    {
                        "id": "1",
                        "key": "DEMO-1",
                        "fields": {
                            "summary": "First",
                            "description": body,
                            "customfield_100": {"items": [1, None]},
                            "customfield_200": 3,
                        },
                    },
                    {"id": "2", "key": "DEMO-2", "fields": {"description": "ghijk", "customfield_100": None}},
                    {"id": "3", "key": "DEMO-3", "fields": {"description": None}},
                    {"id": "4", "key": "DEMO-4"},
                ],
                "names": {
                    "description": "Description",
                    "summary": "Summary",
                    "customfield_100": "Example",
                    "unused": "Unused",
                },
                "schema": {
                    "description": {"type": "any"},
                    "customfield_100": {"type": "array"},
                    "customfield_200": {"type": "number"},
                },
                "nextPageToken": "opaque+token/é=",
                "isLast": False,
            },
        )
    )

    result = await invoke("search_issues", {"jql": "project = DEMO", "include_raw_adf": True, "text_limit": 3})

    assert not result.is_error
    assert result.structured_content == {
        "issues": [
            {
                "id": "1",
                "key": "DEMO-1",
                "fields": {"summary": "First", "customfield_100": {"items": [1, None]}, "customfield_200": 3},
                "field_metadata": {
                    "summary": {"name": "Summary"},
                    "description": {"name": "Description", "schema": {"type": "any"}},
                    "customfield_100": {"name": "Example", "schema": {"type": "array"}},
                    "customfield_200": {"schema": {"type": "number"}},
                },
                "description": {"text": "abc", "characters": 6, "truncated": True, "adf": body},
            },
            {
                "id": "2",
                "key": "DEMO-2",
                "fields": {"customfield_100": None},
                "field_metadata": {
                    "description": {"name": "Description", "schema": {"type": "any"}},
                    "customfield_100": {"name": "Example", "schema": {"type": "array"}},
                },
                "description": {"text": "ghi", "characters": 5, "truncated": True, "adf": "ghijk"},
            },
            {
                "id": "3",
                "key": "DEMO-3",
                "fields": {},
                "field_metadata": {"description": {"name": "Description", "schema": {"type": "any"}}},
                "description": {"text": "", "characters": 0, "truncated": False, "adf": None},
            },
            {"id": "4", "key": "DEMO-4", "fields": {}, "field_metadata": {}},
        ],
        "next_page_token": "opaque+token/é=",
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/rest/api/3/search/jql"
    assert not request.url.params
    assert json.loads(request.content) == {
        "jql": "project = DEMO",
        "maxResults": 20,
        "fields": [
            "summary",
            "status",
            "issuetype",
            "priority",
            "assignee",
            "reporter",
            "labels",
            "created",
            "updated",
            "description",
            "key",
        ],
        "expand": "names,schema",
    }


async def test_issue_search_gateway_and_opaque_json(mock_jira, monkeypatch):
    """Send JQL and continuation tokens unchanged through the Cloud gateway."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_URL", "https://api.atlassian.com/ex/jira/cloud-id/")
    jql = 'project = DEMO AND summary ~ "café + \\"quoted\\""'
    continuation = "opaque+é/=% token"
    responses.append(httpx2.Response(200, json={"issues": [], "isLast": True}))

    result = await invoke(
        "search_issues", {"jql": jql, "next_page_token": continuation, "max_results": 1, "fields": ["id"]}
    )

    assert not result.is_error
    assert result.structured_content == {"issues": [], "next_page_token": None}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/ex/jira/cloud-id/rest/api/3/search/jql"
    assert not request.url.params
    assert json.loads(request.content) == {
        "jql": jql,
        "maxResults": 1,
        "fields": ["id", "key"],
        "expand": "names,schema",
        "nextPageToken": continuation,
    }
    encoded = base64.b64encode(b"reader@example.com:test-secret").decode()
    assert request.headers["authorization"] == f"Basic {encoded}"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (["customfield_100"], ["customfield_100", "key"]),
        (["*all"], ["*all", "key"]),
        (["key", "summary"], ["key", "summary"]),
        (["id"], ["id", "key"]),
        (["key"], ["key"]),
    ],
)
async def test_issue_search_explicit_fields(mock_jira, fields, expected):
    """Replace the defaults while including key exactly once."""
    requests, responses = mock_jira
    original_fields = fields.copy()
    responses.append(httpx2.Response(200, json={"issues": [{"id": "1", "key": "DEMO-1"}]}))

    result = await invoke("search_issues", {"jql": "project = DEMO", "fields": fields, "next_page_token": None})

    assert not result.is_error
    assert result.structured_content == {
        "issues": [{"id": "1", "key": "DEMO-1", "fields": {}, "field_metadata": {}}],
        "next_page_token": None,
    }
    request_data = json.loads(requests[0].content)
    assert request_data["fields"] == expected
    assert fields == original_fields
    assert "nextPageToken" not in request_data
    assert len(requests) == 1


async def test_issue_search_default_text_limit_and_raw_setting(mock_jira):
    """Truncate each description to 2,000 characters and omit raw ADF by default."""
    requests, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={"issues": [{"id": "1", "key": "DEMO-1", "fields": {"description": "x" * 2001}}]},
        )
    )

    result = await invoke("search_issues", {"jql": "project = DEMO"})

    assert not result.is_error
    assert result.structured_content["issues"][0]["description"] == {
        "text": "x" * 2000,
        "characters": 2001,
        "truncated": True,
    }
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("issues", "page", "expected_token"),
    [
        ([], {"nextPageToken": "next-empty"}, "next-empty"),
        ([{"id": "1", "key": "DEMO-1"}], {"nextPageToken": "next-short", "isLast": False}, "next-short"),
        ([], {}, None),
        ([], {"nextPageToken": None, "isLast": True}, None),
    ],
)
async def test_issue_search_continuation_is_driven_by_token(mock_jira, issues, page, expected_token):
    """Continue empty and short pages when Jira supplies an opaque token."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json={"issues": issues, **page}))

    result = await invoke("search_issues", {"jql": "project = DEMO"})

    assert not result.is_error
    assert result.structured_content["next_page_token"] == expected_token
    assert len(result.structured_content["issues"]) == len(issues)
    assert set(result.structured_content) == {"issues", "next_page_token"}
    assert len(requests) == 1


async def test_issue_search_argument_upper_bounds(mock_jira):
    """Accept the documented maximum lengths and page size."""
    requests, responses = mock_jira
    jql = "x" * 4096
    token = "t" * 4096
    fields = [f"customfield_{index}" for index in range(100)]
    page_size = 100
    responses.append(httpx2.Response(200, json={"issues": [], "nextPageToken": token}))

    result = await invoke(
        "search_issues", {"jql": jql, "next_page_token": token, "max_results": page_size, "fields": fields}
    )

    assert not result.is_error
    assert result.structured_content["next_page_token"] == token
    request_data = json.loads(requests[0].content)
    assert request_data["jql"] == jql
    assert request_data["nextPageToken"] == token
    assert request_data["maxResults"] == page_size
    assert request_data["fields"] == [*fields, "key"]
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"issues": None},
        {"issues": {}},
        {"issues": ["test-secret"]},
        {"issues": [{"id": "1", "fields": {}}]},
        {"issues": [{"key": "DEMO-1", "fields": {}}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": None}]},
        {"issues": [{"id": "1", "key": "DEMO-1", "fields": []}]},
        {"issues": [], "names": []},
        {"issues": [], "schema": "test-secret"},
        {"issues": [], "nextPageToken": ""},
        {"issues": [], "nextPageToken": "x" * 4097},
        {"issues": [], "nextPageToken": 123},
        {"issues": [], "isLast": False},
        {"issues": [], "isLast": False, "nextPageToken": None},
        {"issues": [], "isLast": True, "nextPageToken": "test-secret"},
        {"issues": [], "isLast": "false"},
        {"issues": [], "isLast": 0},
        {"issues": [], "isLast": None},
    ],
)
async def test_issue_search_invalid_payloads_are_safe(mock_jira, payload):
    """Reject malformed pages and contradictory pagination without leaking data."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke("search_issues", {"jql": "project = DEMO"})

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
        httpx2.ConnectError("test-secret"),
    ],
)
@pytest.mark.parametrize(
    ("name", "args"), [("search_issues", {"jql": "project = DEMO"}), ("get_issues", {"issues": ["DEMO-1"]})]
)
async def test_post_json_and_transport_errors_are_safe(mock_jira, response, name, args):
    """Sanitize POST decoding and transport errors without making follow-up requests."""
    requests, responses = mock_jira
    responses.append(response)

    result = await invoke(name, args)

    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


async def test_comments_pagination_and_raw_adf(mock_jira):
    """Use actual page length for continuation and preserve original rich text."""
    requests, responses = mock_jira
    body = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "abcdef"}]}]}
    responses.append(
        httpx2.Response(
            200,
            json={
                "startAt": 2,
                "total": 5,
                "comments": [
                    {
                        "id": "9",
                        "author": {"accountId": "abc", "displayName": "Reader", "avatarUrls": {}},
                        "created": "now",
                        "updated": "later",
                        "body": body,
                    }
                ],
            },
        )
    )

    result = await invoke(
        "get_issue_comments",
        {
            "issue": "DEMO-1",
            "start_at": 2,
            "max_results": 10,
            "text_limit": 3,
            "include_raw_adf": True,
        },
    )

    data = result.structured_content
    assert data["next_start_at"] == data["start_at"] + len(data["comments"])
    assert data["comments"][0]["body"] == {"text": "abc", "characters": 6, "truncated": True, "adf": body}
    assert data["comments"][0]["author"] == {"accountId": "abc", "displayName": "Reader"}
    assert requests[0].url.params["orderBy"] == "created"


@pytest.mark.parametrize(("start", "total"), [(0, 0), (8, 10)])
async def test_empty_comment_page_terminates(mock_jira, start, total):
    """Avoid a continuation loop when comments disappear during pagination."""
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json={"startAt": start, "total": total, "comments": []}))

    result = await invoke("get_issue_comments", {"issue": "DEMO-1", "start_at": start})

    assert result.structured_content["next_start_at"] is None


@pytest.mark.parametrize("project", ["TEAM_2", "10042"])
async def test_create_metadata_issue_types_are_compact_and_fresh(mock_jira, project):
    """Return Jira's ordered issue types without avatar noise or cached results."""
    requests, responses = mock_jira
    items = [
        {"id": "5", "name": "Task", "description": "A task", "subtask": False},
        {"id": "8", "name": "Subtask"},
    ]
    page = {
        "startAt": 3,
        "maxResults": 2,
        "total": 7,
        "issueTypes": [{**item, "avatarId": 123, "self": "unneeded"} for item in items],
    }
    responses.extend([httpx2.Response(200, json=page), httpx2.Response(200, json=page)])

    async with Client(mcp) as client:
        first = await client.call_tool(
            "list_project_issue_types", {"project": project, "start_at": 2, "max_results": 10}
        )
        second = await client.call_tool(
            "list_project_issue_types", {"project": project, "start_at": 2, "max_results": 10}
        )

    assert first.structured_content == {
        "issue_types": items,
        "start_at": 3,
        "max_results": 2,
        "total": 7,
        "next_start_at": 5,
    }
    assert second.structured_content == first.structured_content
    expected_requests = 2
    assert len(requests) == expected_requests
    for request in requests:
        assert request.method == "GET"
        assert request.url.path == f"/rest/api/3/issue/createmeta/{project}/issuetypes"
        assert dict(request.url.params) == {"startAt": "2", "maxResults": "10"}


async def test_create_metadata_fields_preserve_identity_and_original_json(mock_jira):
    """Keep duplicate names, nested options, and the distinction between null and missing defaults."""
    requests, responses = mock_jira
    fields = [
        {
            "fieldId": "customfield_2",
            "key": "customfield_2",
            "name": "Team",
            "required": True,
            "schema": {"type": "option", "nested": {"value": None}},
            "operations": ["set", "add"],
            "hasDefaultValue": True,
            "defaultValue": None,
            "allowedValues": [{"id": "3", "child": {"value": "Nested", "extra": None}}],
            "autoCompleteUrl": "https://example.atlassian.net/options",
        },
        {
            "fieldId": "customfield_1",
            "key": "customfield_1",
            "name": "Team",
            "required": False,
            "schema": None,
            "hasDefaultValue": False,
        },
        {"key": "summary", "name": "Summary", "required": True, "defaultValue": {"nested": [None, 0]}},
    ]
    responses.append(
        httpx2.Response(
            200,
            json={
                "startAt": 0,
                "maxResults": 50,
                "total": 3,
                "fields": [{**field, "unneeded": "ignore"} for field in fields],
            },
        )
    )

    result = await invoke("list_create_fields", {"project": "10042", "issue_type_id": "9"})

    assert not result.is_error
    assert result.structured_content == {
        "fields": fields,
        "start_at": 0,
        "max_results": 50,
        "total": 3,
        "next_start_at": None,
    }
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/issue/createmeta/10042/issuetypes/9"
    assert dict(requests[0].url.params) == {"startAt": "0", "maxResults": "50"}


async def test_create_metadata_fields_are_fresh(mock_jira):
    """Fetch create-field metadata again rather than reusing a cached page."""
    requests, responses = mock_jira
    for name in ("First", "Updated"):
        responses.append(
            httpx2.Response(
                200,
                json={
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 1,
                    "fields": [{"fieldId": "summary", "name": name, "required": True}],
                },
            )
        )

    async with Client(mcp) as client:
        args = {"project": "TEAM", "issue_type_id": "9"}
        first = await client.call_tool("list_create_fields", args)
        second = await client.call_tool("list_create_fields", args)

    assert first.structured_content["fields"][0]["name"] == "First"
    assert second.structured_content["fields"][0]["name"] == "Updated"
    expected_requests = 2
    assert len(requests) == expected_requests


async def test_create_metadata_argument_upper_bounds(mock_jira):
    """Accept the documented maximum path lengths and both page-size bounds."""
    requests, responses = mock_jira
    project = "A" * 255
    issue_type = "1" * 255
    responses.extend(
        [
            httpx2.Response(200, json={"startAt": 0, "maxResults": 1, "total": 0, "issueTypes": []}),
            httpx2.Response(200, json={"startAt": 0, "maxResults": 100, "total": 0, "fields": []}),
        ]
    )

    types = await invoke("list_project_issue_types", {"project": project, "max_results": 1})
    fields = await invoke("list_create_fields", {"project": project, "issue_type_id": issue_type, "max_results": 100})

    assert not types.is_error
    assert not fields.is_error
    expected_requests = 2
    assert len(requests) == expected_requests
    assert requests[0].url.path == f"/rest/api/3/issue/createmeta/{project}/issuetypes"
    assert requests[1].url.path == f"/rest/api/3/issue/createmeta/{project}/issuetypes/{issue_type}"


@pytest.mark.parametrize(
    "tool_case",
    [
        ("list_project_issue_types", {"project": "TEAM"}, "issueTypes", {"id": "1", "name": "Task"}),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            "fields",
            {
                "fieldId": "summary",
                "name": "Summary",
                "required": True,
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "page_case",
    [(4, 1, 9, 5), (4, 0, 9, None), (4, 1, 5, None), (5, 1, 5, None)],
)
async def test_create_metadata_pagination(mock_jira, tool_case, page_case):
    """Advance from Jira's actual start and raw count, including short and empty pages."""
    requests, responses = mock_jira
    name, args, item_key, item = tool_case
    start, count, total, expected_next = page_case
    page_size = 10
    responses.append(
        httpx2.Response(200, json={"startAt": start, "maxResults": page_size, "total": total, item_key: [item] * count})
    )

    result = await invoke(name, {**args, "start_at": 2, "max_results": 100})

    assert not result.is_error
    assert result.structured_content["start_at"] == start
    assert result.structured_content["max_results"] == page_size
    assert result.structured_content["total"] == total
    assert result.structured_content["next_start_at"] == expected_next
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"startAt": "2", "maxResults": "100"}


@pytest.mark.parametrize(
    ("name", "args", "payload"),
    [
        ("list_project_issue_types", {"project": "TEAM"}, {}),
        ("list_project_issue_types", {"project": "TEAM"}, {"startAt": 0, "maxResults": 50, "total": 1}),
        (
            "list_project_issue_types",
            {"project": "TEAM"},
            {
                "startAt": -1,
                "maxResults": 50,
                "total": 1,
                "issueTypes": [],
            },
        ),
        (
            "list_project_issue_types",
            {"project": "TEAM"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "issueTypes": [{"id": "1", "name": ["test-secret"]}],
            },
        ),
        (
            "list_project_issue_types",
            {"project": "TEAM"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "issueTypes": [{"id": "1", "name": "Task", "subtask": "no"}],
            },
        ),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "fields": [{"name": "test-secret", "required": True}],
            },
        ),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "fields": [{"fieldId": "x", "name": "test-secret", "required": "yes"}],
            },
        ),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": 1,
                "fields": [{"fieldId": "x", "name": "test-secret", "required": True, "schema": []}],
            },
        ),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            {
                "startAt": 0,
                "maxResults": 50,
                "total": -1,
                "fields": [],
            },
        ),
        (
            "list_create_fields",
            {"project": "TEAM", "issue_type_id": "9"},
            {
                "startAt": 0,
                "maxResults": "test-secret",
                "total": 0,
                "fields": [],
            },
        ),
    ],
)
async def test_create_metadata_invalid_payload_is_safe(mock_jira, name, args, payload):
    """Reject malformed metadata and page envelopes without leaking upstream content."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke(name, args)

    assert result.is_error
    assert "unexpected response shape" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_project_issue_types", {"project": "TEAM"}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "9"}),
    ],
)
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, text="test-secret"),
        httpx2.Response(200, text="test-secret"),
        httpx2.ReadTimeout("test-secret"),
    ],
)
async def test_create_metadata_http_errors_are_safe(mock_jira, name, args, response):
    """Sanitize HTTP, JSON, and transport failures with no additional requests."""
    requests, responses = mock_jira
    responses.append(response)

    result = await invoke(name, args)

    assert result.is_error
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


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


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_issue", {"issue": "../bad"}),
        ("get_issue", {"issue": "DEMO-1", "fields": []}),
        ("get_issue", {"issue": "DEMO-1", "fields": ["summary,comment"]}),
        ("get_issues", {}),
        ("get_issues", {"issues": None}),
        ("get_issues", {"issues": []}),
        ("get_issues", {"issues": ["DEMO-1"] * 21}),
        ("get_issues", {"issues": [f"DEMO-{index}" for index in range(1, 22)]}),
        ("get_issues", {"issues": ["DEMO-1", "../bad"]}),
        ("get_issues", {"issues": ["DEMO-0"]}),
        ("get_issues", {"issues": ["a" * 256]}),
        ("get_issues", {"issues": ["DEMO-1"], "fields": []}),
        ("get_issues", {"issues": ["DEMO-1"], "fields": ["summary,comment"]}),
        ("get_issues", {"issues": ["DEMO-1"], "fields": ["summary"] * 101}),
        ("get_issues", {"issues": ["DEMO-1"], "text_limit": 0}),
        ("get_issues", {"issues": ["DEMO-1"], "text_limit": 100_001}),
        ("get_issue_comments", {"issue": "DEMO-1", "start_at": -1}),
        ("get_issue_comments", {"issue": "DEMO-1", "max_results": 101}),
        ("get_issue", {"issue": "DEMO-1", "text_limit": 0}),
        ("list_fields", {"issue": "../bad"}),
        ("list_fields", {"issue": "DEMO-0"}),
        ("list_fields", {"issue": "a" * 256}),
        ("list_fields", {"start_at": -1}),
        ("list_fields", {"max_results": 101}),
        ("list_fields", {"refresh": "not-a-bool"}),
        ("list_project_issue_types", {}),
        ("list_project_issue_types", {"project": "../bad"}),
        ("list_project_issue_types", {"project": "TEAM/other"}),
        ("list_project_issue_types", {"project": "TEAM%2Fother"}),
        ("list_project_issue_types", {"project": "TEAM\n"}),
        ("list_project_issue_types", {"project": "TEAM-1"}),
        ("list_project_issue_types", {"project": "0"}),
        ("list_project_issue_types", {"project": "01"}),
        ("list_project_issue_types", {"project": "A" * 256}),
        ("list_project_issue_types", {"project": "TEAM", "start_at": -1}),
        ("list_project_issue_types", {"project": "TEAM", "max_results": 0}),
        ("list_project_issue_types", {"project": "TEAM", "max_results": 101}),
        ("list_create_fields", {"project": "TEAM"}),
        ("list_create_fields", {"project": "../bad", "issue_type_id": "1"}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "../1"}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "0"}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "01"}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "1" * 256}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "1", "start_at": -1}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "1", "max_results": 0}),
        ("list_create_fields", {"project": "TEAM", "issue_type_id": "1", "max_results": 101}),
        ("search_users", {}),
        ("search_users", {"query": ""}),
        ("search_users", {"query": " \t\n"}),
        ("search_users", {"query": "a" * 256}),
        ("search_users", {"query": "Reader", "start_at": -1}),
        ("search_users", {"query": "Reader", "start_at": 1000}),
        ("search_users", {"query": "Reader", "max_results": 0}),
        ("search_users", {"query": "Reader", "max_results": 101}),
        ("search_issues", {}),
        ("search_issues", {"jql": None}),
        ("search_issues", {"jql": ""}),
        ("search_issues", {"jql": " \t\n"}),
        ("search_issues", {"jql": "x" * 4097}),
        ("search_issues", {"jql": "project = DEMO", "next_page_token": ""}),
        ("search_issues", {"jql": "project = DEMO", "next_page_token": " \t\n"}),
        ("search_issues", {"jql": "project = DEMO", "next_page_token": "x" * 4097}),
        ("search_issues", {"jql": "project = DEMO", "max_results": 0}),
        ("search_issues", {"jql": "project = DEMO", "max_results": 101}),
        ("search_issues", {"jql": "project = DEMO", "fields": []}),
        ("search_issues", {"jql": "project = DEMO", "fields": ["summary,comment"]}),
        ("search_issues", {"jql": "project = DEMO", "fields": ["summary"] * 101}),
        ("search_issues", {"jql": "project = DEMO", "text_limit": 0}),
        ("search_issues", {"jql": "project = DEMO", "text_limit": 100_001}),
    ],
)
async def test_bad_arguments_do_not_request(mock_jira, name, args):
    """Reject unsafe paths and unbounded or ambiguous selection."""
    requests, _ = mock_jira

    result = await invoke(name, args)

    assert result.is_error
    assert requests == []


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500])
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_issue", {"issue": "DEMO-1"}),
        ("get_issues", {"issues": ["DEMO-1"]}),
        ("search_issues", {"jql": "project = DEMO"}),
    ],
)
async def test_http_errors_are_safe(mock_jira, status, name, args):
    """Do not expose response bodies or follow redirects with credentials."""
    requests, responses = mock_jira
    responses.append(httpx2.Response(status, text="test-secret", headers={"location": "https://evil.invalid/"}))

    result = await invoke(name, args)

    assert result.is_error
    assert str(status) in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="test-secret"),
        httpx2.Response(200, json=[]),
        httpx2.Response(200, json={"fields": []}),
        httpx2.ReadTimeout("test-secret"),
        httpx2.ConnectError("test-secret"),
    ],
)
async def test_transport_and_payload_errors_are_safe(mock_jira, response):
    """Sanitize failures at the HTTP boundary."""
    _, responses = mock_jira
    responses.append(response)

    result = await invoke("get_issue", {"issue": "DEMO-1"})

    assert result.is_error
    assert "test-secret" not in result.content[0].text


async def test_discovery_without_credentials(monkeypatch):
    """Let agents discover tools and get actionable configuration errors."""
    monkeypatch.delenv("JIRA_API_TOKEN")

    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool("list_fields", {}, raise_on_error=False)
        issue_result = await client.call_tool("list_fields", {"issue": "DEMO-1"}, raise_on_error=False)

    assert {tool.name for tool in tools} == {
        "get_issue",
        "get_issues",
        "get_issue_comments",
        "list_fields",
        "list_project_issue_types",
        "list_create_fields",
        "get_current_user",
        "search_users",
        "search_issues",
    }
    assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint for tool in tools)
    schemas = {tool.name: tool.model_dump(by_alias=True)["inputSchema"] for tool in tools}
    issue_description = next(tool.description for tool in tools if tool.name == "get_issue")
    assert issue_description is not None
    assert "field_metadata instead of extra lookups" in issue_description
    assert all("ctx" not in schema["properties"] for schema in schemas.values())
    assert schemas["get_issue"]["required"] == ["issue"]
    assert schemas["get_issue"]["properties"]["fields"]["anyOf"][0]["minItems"] == 1
    bulk_schema = schemas["get_issues"]
    assert bulk_schema["required"] == ["issues"]
    bulk_properties = bulk_schema["properties"]
    assert {"type": "array", "minItems": 1, "maxItems": 20}.items() <= bulk_properties["issues"].items()
    assert bulk_properties["issues"]["items"]["pattern"] == schemas["get_issue"]["properties"]["issue"]["pattern"]
    assert {"maxLength": 255}.items() <= bulk_properties["issues"]["items"].items()
    assert {"minItems": 1, "maxItems": 100}.items() <= bulk_properties["fields"]["anyOf"][0].items()
    assert bulk_properties["include_raw_adf"]["default"] is False
    assert {"default": 12_000, "minimum": 1, "maximum": 100_000}.items() <= bulk_properties["text_limit"].items()
    assert schemas["search_users"]["required"] == ["query"]
    assert {"maximum": 999, "minimum": 0}.items() <= schemas["search_users"]["properties"]["start_at"].items()
    assert {"default": 20, "minimum": 1, "maximum": 100}.items() <= schemas["search_users"]["properties"][
        "max_results"
    ].items()
    search_schema = schemas["search_issues"]
    assert search_schema["required"] == ["jql"]
    properties = search_schema["properties"]
    assert {"type": "string", "maxLength": 4096}.items() <= properties["jql"].items()
    assert "pattern" in properties["jql"]
    assert {"type": "string", "minLength": 1, "maxLength": 4096}.items() <= properties["next_page_token"]["anyOf"][
        0
    ].items()
    assert "pattern" in properties["next_page_token"]["anyOf"][0]
    assert {"default": 20, "minimum": 1, "maximum": 100}.items() <= properties["max_results"].items()
    assert {"minItems": 1, "maxItems": 100}.items() <= properties["fields"]["anyOf"][0].items()
    assert properties["include_raw_adf"]["default"] is False
    assert {"default": 2000, "minimum": 1, "maximum": 100_000}.items() <= properties["text_limit"].items()
    assert not schemas["get_current_user"].get("required")
    assert not schemas["list_fields"].get("required")
    assert schemas["list_fields"]["properties"]["refresh"]["default"] is False
    assert schemas["list_fields"]["properties"]["issue"]["default"] is None
    assert result.is_error
    assert "JIRA_API_TOKEN" in result.content[0].text
    assert issue_result.is_error
    assert "JIRA_API_TOKEN" in issue_result.content[0].text


async def test_create_metadata_schemas_and_annotations():
    """Advertise bounded inputs and read-only annotations for both metadata tools."""
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    names = ("list_project_issue_types", "list_create_fields")
    schemas = {name: tools[name].model_dump(by_alias=True)["inputSchema"] for name in names}
    assert schemas[names[0]]["required"] == ["project"]
    assert schemas[names[1]]["required"] == ["project", "issue_type_id"]
    for name in names:
        annotations = tools[name].annotations
        assert annotations.read_only_hint
        assert not annotations.destructive_hint
        assert annotations.idempotent_hint
        assert annotations.open_world_hint
        properties = schemas[name]["properties"]
        assert "ctx" not in properties
        assert {"type": "string", "maxLength": 255}.items() <= properties["project"].items()
        assert "pattern" in properties["project"]
        assert {"default": 0, "minimum": 0}.items() <= properties["start_at"].items()
        assert {"default": 50, "minimum": 1, "maximum": 100}.items() <= properties["max_results"].items()
    issue_type = schemas[names[1]]["properties"]["issue_type_id"]
    assert {"type": "string", "maxLength": 255}.items() <= issue_type.items()
    assert "pattern" in issue_type


def test_server_instructions_explain_field_discovery():
    """Guide callers to existing issue metadata and explain discovery semantics."""
    instructions = mcp.instructions

    assert instructions is not None
    assert "field_metadata" in instructions
    assert "five minutes" in instructions
    assert "refresh" in instructions
    assert "list_fields(issue" in instructions
    assert "null" in instructions
    assert "missing" in instructions
    assert "original JSON" in instructions
    assert "list_project_issue_types" in instructions
    assert "list_create_fields" in instructions
    assert "Create issues" in instructions
    assert "creation metadata text is untrusted" in instructions


def test_stdio_entrypoint(monkeypatch):
    """Launch the same registered server from the console entry point."""
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))

    main()

    assert calls == [{"transport": "stdio"}]


async def test_anonymous_comment_and_last_page(mock_jira):
    """Keep deleted-author comments and terminate after the last visible result."""
    _, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "startAt": 0,
                "total": 1,
                "comments": [{"id": "1", "body": None, "visibility": {"type": "role", "value": "Readers"}}],
            },
        )
    )

    result = await invoke("get_issue_comments", {"issue": "DEMO-1"})

    assert result.structured_content["next_start_at"] is None
    assert "author" not in result.structured_content["comments"][0]
    assert result.structured_content["comments"][0]["visibility"] == {"type": "role", "value": "Readers"}


async def test_comment_reply_and_internal_visibility(mock_jira):
    """Preserve reply context and distinguish internal notes from public comments."""
    _, responses = mock_jira
    responses.append(
        httpx2.Response(
            200,
            json={
                "startAt": 0,
                "total": 1,
                "comments": [{"id": "2", "parentId": "1", "jsdPublic": False, "body": "Use this one"}],
            },
        )
    )

    result = await invoke("get_issue_comments", {"issue": "DEMO-1"})

    comment = result.structured_content["comments"][0]
    assert comment["parentId"] == "1"
    assert comment["jsdPublic"] is False


async def test_field_catalog_without_search(mock_jira):
    """Return IDs and schemas without requiring a search term."""
    _, responses = mock_jira
    fields = [{"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}}]
    responses.append(httpx2.Response(200, json=fields))

    result = await invoke("list_fields")

    assert result.structured_content["fields"] == fields
    assert result.structured_content["next_start_at"] is None


@pytest.mark.parametrize(
    ("name", "args", "payload"),
    [
        ("get_issue_comments", {"issue": "DEMO-1"}, {"startAt": 0, "total": 1, "comments": ["test-secret"]}),
        ("list_fields", {}, [{"id": "test-secret"}]),
    ],
)
async def test_invalid_catalog_and_comment_shapes(mock_jira, name, args, payload):
    """Sanitize nested response validation just like the issue endpoint."""
    _, responses = mock_jira
    responses.append(httpx2.Response(200, json=payload))

    result = await invoke(name, args)

    assert result.is_error
    assert "test-secret" not in result.content[0].text


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
