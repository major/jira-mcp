"""Exercise issue, bulk fetch, search, and comment tools through the MCP and Jira boundaries 🎫."""

import base64
import json

import httpx2
import pytest

from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


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
