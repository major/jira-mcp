"""Exercise creation metadata and issue creation through the MCP and Jira boundaries ✨."""

import json

import httpx2
import pytest
from fastmcp import Client

from jira_mcp import mcp
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


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


async def test_create_disabled_and_read_post_tools_still_work(mock_jira):
    """Keep creation disabled by default without blocking read-only POST calls."""
    requests, responses = mock_jira
    responses.extend([httpx2.Response(200, json={"issues": []}), httpx2.Response(200, json={"issues": []})])

    async with Client(mcp) as client:
        tools = await client.list_tools()
        disabled = await client.call_tool(
            "create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"}, raise_on_error=False
        )
        bulk = await client.call_tool("get_issues", {"issues": ["TEAM-1"]})
        search = await client.call_tool("search_issues", {"jql": "project = TEAM"})

    assert disabled.is_error
    assert any(tool.name == "create_issue" for tool in tools)
    assert "JIRA_ENABLE_ISSUE_CREATION" in disabled.content[0].text
    assert "not confirmed" not in disabled.content[0].text
    assert not bulk.is_error
    assert not search.is_error
    assert [request.url.path for request in requests] == ["/rest/api/3/issue/bulkfetch", "/rest/api/3/search/jql"]


async def test_create_settings_are_captured_per_lifespan(mock_jira, monkeypatch):
    """Changing the environment after startup cannot enable issue creation in an existing lifespan."""
    requests, _ = mock_jira

    async with Client(mcp) as client:
        monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
        result = await client.call_tool(
            "create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"}, raise_on_error=False
        )

    assert result.is_error
    assert "JIRA_ENABLE_ISSUE_CREATION" in result.content[0].text
    assert requests == []


async def test_create_missing_configuration_does_not_request(mock_jira, monkeypatch):
    """Report missing credentials before attempting creation."""
    requests, _ = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    monkeypatch.delenv("JIRA_API_TOKEN")

    result = await invoke("create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"})

    assert result.is_error
    assert "JIRA_API_TOKEN" in result.content[0].text
    assert "not confirmed" not in result.content[0].text
    assert requests == []


async def test_create_invalid_issue_creation_configuration_does_not_request(mock_jira, monkeypatch):
    """Expose a safe configuration error without making a creation request."""
    requests, _ = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "test-secret")

    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"}, raise_on_error=False
        )

    assert any(tool.name == "create_issue" for tool in tools)
    assert result.is_error
    assert "JIRA_ENABLE_ISSUE_CREATION" in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert "not confirmed" not in result.content[0].text
    assert requests == []


def create_metadata(*field_ids, **overrides):
    """Return one create-field metadata page listing summary and each supplied field ID."""
    fields = [{"fieldId": field_id, "name": "Name", "required": False} for field_id in ("summary", *field_ids)]
    fields += [{"fieldId": field_id, "name": "Name", **value} for field_id, value in overrides.items()]
    return httpx2.Response(200, json={"startAt": 0, "maxResults": 100, "total": len(fields), "fields": fields})


@pytest.mark.parametrize(
    ("project", "options", "expected_fields"),
    [
        ("TEAM", {}, {"project": {"key": "TEAM"}}),
        ("10042", {"labels": []}, {"project": {"id": "10042"}, "labels": []}),
        (
            "TEAM_2",
            {"labels": ["first", "café"], "priority_id": "3", "assignee_account_id": "712020:abc", "parent": "TEAM-1"},
            {
                "project": {"key": "TEAM_2"},
                "labels": ["first", "café"],
                "priority": {"id": "3"},
                "assignee": {"accountId": "712020:abc"},
                "parent": {"key": "TEAM-1"},
            },
        ),
        (
            "TEAM",
            {
                "description": "hello\r\n\n🙂",
                "parent": "10001",
                "additional_fields": {
                    "customfield_97531": "Epic title",
                    "components": [{"id": "5"}],
                    "customfield_20": None,
                    "environment": {"type": "doc", "version": 1, "content": [{"type": "paragraph"}]},
                },
            },
            {
                "project": {"key": "TEAM"},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]},
                        {"type": "paragraph"},
                        {"type": "paragraph", "content": [{"type": "text", "text": "🙂"}]},
                    ],
                },
                "parent": {"id": "10001"},
                "customfield_97531": "Epic title",
                "components": [{"id": "5"}],
                "customfield_20": None,
                "environment": {"type": "doc", "version": 1, "content": [{"type": "paragraph"}]},
            },
        ),
        (
            "TEAM",
            {"description": ""},
            {
                "project": {"key": "TEAM"},
                "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph"}]},
            },
        ),
    ],
)
async def test_create_issue_payload_and_single_post(mock_jira, monkeypatch, project, options, expected_fields):
    """Validate against fresh metadata, then send exactly one creation request and return the issue URL."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "TRUE")
    supplied = [field_id for field_id in expected_fields if field_id not in ("project", "parent")]
    responses.extend(
        [create_metadata(*supplied), httpx2.Response(201, json={"id": "123", "key": "TEAM-8", "self": "not-returned"})]
    )

    result = await invoke("create_issue", {"project": project, "issue_type_id": "42", "summary": "Title", **options})

    assert not result.is_error
    assert result.structured_content == {
        "id": "123",
        "key": "TEAM-8",
        "url": "https://example.atlassian.net/browse/TEAM-8",
    }
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/rest/api/3/issue/createmeta/{project}/issuetypes/42"),
        ("POST", "/rest/api/3/issue"),
    ]
    assert requests[0].url.params["maxResults"] == "100"
    assert json.loads(requests[1].content) == {
        "fields": {
            "issuetype": {"id": "42"},
            "summary": "Title",
            **expected_fields,
        }
    }


async def test_create_through_gateway_returns_no_browse_url(mock_jira, monkeypatch):
    """Avoid inventing a browse link when only the scoped-token gateway URL is known."""
    _, responses = mock_jira
    monkeypatch.setenv("JIRA_URL", "https://api.atlassian.com/ex/jira/cloud-id")
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    responses.extend([create_metadata(), httpx2.Response(201, json={"id": "123", "key": "TEAM-8"})])

    result = await invoke("create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"})

    assert result.structured_content == {"id": "123", "key": "TEAM-8", "url": None}


@pytest.mark.parametrize("enabled", ["true", "false"])
async def test_create_validate_only_previews_without_writing(mock_jira, monkeypatch, enabled):
    """Return the exact payload after reading metadata, with no POST even when creation is enabled."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", enabled)
    responses.append(create_metadata("labels", "priority", reporter={"required": True, "hasDefaultValue": True}))

    result = await invoke(
        "create_issue",
        {"project": "TEAM", "issue_type_id": "9", "summary": "Title", "labels": ["a"], "validate_only": True},
    )

    assert not result.is_error
    assert result.structured_content == {
        "validate_only": True,
        "creation_enabled": enabled == "true",
        "fields": {"project": {"key": "TEAM"}, "issuetype": {"id": "9"}, "summary": "Title", "labels": ["a"]},
    }
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("allowed", "value"),
    [
        ([{"id": 1, "value": "One"}], {"id": "1"}),
        ([{"id": "1", "value": "High"}], {"value": "high", "child": {"value": "not listed"}}),
        ([{"id": "1", "name": "Web"}], [{"value": "anything"}, {"name": "web"}]),
        ([], {"id": "unlisted"}),
        (["set"], {"id": "unlisted"}),
        ([{"id": "1"}], "free-form"),
    ],
)
@pytest.mark.parametrize("auto_complete", [{}, {"autoCompleteUrl": "https://example.atlassian.net/x"}])
async def test_create_option_checks_are_lenient(mock_jira, allowed, value, auto_complete):
    """Never reject option shapes that Jira itself might accept."""
    _, responses = mock_jira
    responses.append(create_metadata(customfield_1={"required": False, "allowedValues": allowed, **auto_complete}))

    result = await invoke(
        "create_issue",
        {
            "project": "TEAM",
            "issue_type_id": "9",
            "summary": "Title",
            "additional_fields": {"customfield_1": value},
            "validate_only": True,
        },
    )

    assert not result.is_error


@pytest.mark.parametrize(
    ("metadata", "options", "reason"),
    [
        (
            [create_metadata(customfield_1={"required": True}, **{"bad id": {"required": True}})],
            {},
            "customfield_1 is required; a field with an unusual ID is required",
        ),
        ([create_metadata()], {"priority_id": "3"}, "priority is not available"),
        (
            [create_metadata(priority={"required": False, "allowedValues": [{"id": "1", "name": "test-secret"}]})],
            {"priority_id": "3", "validate_only": True},
            "priority has a value that is not allowed",
        ),
        (
            [httpx2.Response(200, json={"startAt": 0, "maxResults": 100, "total": 0, "fields": []})],
            {},
            "Create issues permission",
        ),
        (
            [
                httpx2.Response(
                    200,
                    json={
                        "startAt": 0,
                        "maxResults": 1,
                        "total": 99,
                        "fields": [{"fieldId": "summary", "name": "Summary", "required": True}],
                    },
                )
            ]
            * 10,
            {},
            "too much creation metadata",
        ),
        ([httpx2.Response(403, text="test-secret")], {}, "HTTP status 403"),
        ([httpx2.Response(200, json={"private": "test-secret"})], {}, "unexpected response shape. Check"),
    ],
)
async def test_create_rejects_before_post(mock_jira, monkeypatch, metadata, options, reason):
    """Report metadata problems with safe field IDs and never send a creation request."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    responses.extend(metadata)

    result = await invoke(
        "create_issue",
        {"project": "TEAM", "issue_type_id": "9", "summary": "Title", **options},
    )

    assert result.is_error
    message = result.content[0].text
    assert message.startswith("No issue was created: ")
    assert reason in message
    assert "test-secret" not in message
    assert {request.method for request in requests} == {"GET"}
    assert len(requests) == len(metadata)


@pytest.mark.parametrize("has_epic_name", [True, False])
async def test_create_epic_with_discovered_fields(mock_jira, monkeypatch, has_epic_name):
    """Use a discovered Epic Name ID only when supplied, with no implicit field matching."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    fields = [{"fieldId": "summary", "name": "Summary", "required": True}]
    fields += [{"fieldId": "customfield_97531", "name": "Epic Name", "required": False}] if has_epic_name else []
    metadata = httpx2.Response(200, json={"startAt": 0, "maxResults": 50, "total": len(fields), "fields": fields})
    responses.extend(
        [
            httpx2.Response(
                200, json={"startAt": 0, "maxResults": 50, "total": 1, "issueTypes": [{"id": "42", "name": "Epic"}]}
            ),
            metadata,
            metadata,
            httpx2.Response(201, json={"id": "123", "key": "TEAM-8"}),
        ]
    )

    async with Client(mcp) as client:
        types = await client.call_tool("list_project_issue_types", {"project": "TEAM"})
        issue_type_id = types.structured_content["issue_types"][0]["id"]
        metadata = await client.call_tool("list_create_fields", {"project": "TEAM", "issue_type_id": issue_type_id})
        supplied = {metadata.structured_content["fields"][-1]["fieldId"]: "Epic title"} if has_epic_name else {}
        result = await client.call_tool(
            "create_issue",
            {"project": "TEAM", "issue_type_id": issue_type_id, "summary": "Epic title", "additional_fields": supplied},
        )

    assert not result.is_error
    assert [request.method for request in requests] == ["GET", "GET", "GET", "POST"]
    assert json.loads(requests[-1].content)["fields"] == {
        "project": {"key": "TEAM"},
        "issuetype": {"id": "42"},
        "summary": "Epic title",
        **supplied,
    }


@pytest.mark.parametrize(
    "reserved", ["project", "issuetype", "summary", "description", "labels", "priority", "assignee", "parent"]
)
async def test_create_rejects_reserved_fields_before_http(mock_jira, monkeypatch, reserved):
    """Reject every core field collision, including when optional arguments are omitted."""
    requests, _ = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")

    result = await invoke(
        "create_issue",
        {
            "project": "TEAM",
            "issue_type_id": "9",
            "summary": "Title",
            "additional_fields": {reserved: None},
        },
    )

    assert result.is_error
    assert requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"project": "../bad"},
        {"project": "0"},
        {"issue_type_id": "Epic"},
        {"issue_type_id": "0"},
        {"summary": ""},
        {"summary": " \t\n"},
        {"summary": "Title\nmore"},
        {"summary": "Title\r"},
        {"summary": "Title\n"},
        {"summary": "x" * 256},
        {"summary": None},
        {"description": "x" * 100_001},
        {"labels": ["bad label"]},
        {"labels": [""]},
        {"labels": ["bad\tlabel"]},
        {"labels": ["x" * 256]},
        {"labels": ["x"] * 101},
        {"priority_id": "High"},
        {"assignee_account_id": "bad id"},
        {"assignee_account_id": "x" * 129},
        {"parent": "../bad"},
        {"additional_fields": {"*all": 1}},
        {"additional_fields": {"bad.field": 1}},
        {"additional_fields": {"x" * 256: 1}},
        {"additional_fields": {f"field_{i}": i for i in range(101)}},
    ],
)
async def test_create_invalid_arguments_do_not_request(mock_jira, monkeypatch, changes):
    """Reject invalid or unbounded creation inputs locally."""
    requests, _ = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")

    result = await invoke("create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title", **changes})

    assert result.is_error
    assert requests == []


async def test_create_accepts_upper_bounds(mock_jira, monkeypatch):
    """Accept the maximum documented lengths and number of labels and additional fields."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    options = {
        "project": "A" * 255,
        "issue_type_id": "1" * 255,
        "summary": "s" * 255,
        "description": "d" * 100_000,
        "labels": ["l" * 255] * 100,
        "assignee_account_id": "a" * 128,
        "additional_fields": {f"field_{i}": [i, None] for i in range(100)},
    }
    fields = create_metadata("description", "labels", "assignee", *options["additional_fields"]).json()["fields"]
    pages = [
        httpx2.Response(200, json={"startAt": start, "maxResults": 100, "total": len(fields), "fields": page})
        for start, page in ((0, fields[:100]), (100, fields[100:]))
    ]
    responses.extend([*pages, httpx2.Response(201, json={"id": "1", "key": "TEAM-1"})])

    result = await invoke("create_issue", options)

    assert not result.is_error
    sent = json.loads(requests[-1].content)["fields"]
    assert sent["summary"] == options["summary"]
    assert sent["description"]["content"][0]["content"][0]["text"] == options["description"]
    assert sent["labels"] == options["labels"]
    assert sent["assignee"] == {"accountId": options["assignee_account_id"]}
    assert all(sent[key] == value for key, value in options["additional_fields"].items())
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert requests[1].url.params["startAt"] == "100"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.ReadTimeout("test-secret"), "network or timeout"),
        (httpx2.ConnectError("test-secret"), "network or timeout"),
        (httpx2.Response(500, text="test-secret"), "HTTP status 500"),
        (httpx2.Response(409, text="test-secret"), "HTTP status 409"),
        (httpx2.Response(201, text="test-secret"), "invalid JSON"),
        (
            httpx2.Response(201, json={"id": "0", "key": "TEAM-1", "private": "test-secret"}),
            "unexpected creation response",
        ),
        (httpx2.Response(201, json={"id": "1", "key": "", "private": "test-secret"}), "unexpected creation response"),
        (httpx2.Response(201, json={"id": 1, "key": "TEAM-1"}), "unexpected creation response"),
        (httpx2.Response(201, json={"id": "1", "key": "bad key"}), "unexpected creation response"),
    ],
)
async def test_create_uncertain_errors_are_safe_and_not_retried(mock_jira, monkeypatch, response, reason):
    """Warn about possible creation after a failed request without exposing upstream details."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    responses.extend([create_metadata(), response])

    result = await invoke("create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"})

    assert result.is_error
    message = result.content[0].text
    assert reason in message
    assert "not confirmed" in message
    assert "may already exist" in message
    assert "verify Jira before retrying" in message
    assert "test-secret" not in message
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (400, "field values"),
        (401, "credentials"),
        (403, "Create issues permission"),
        (404, "Jira URL"),
        (422, "project configuration"),
        (429, "wait"),
    ],
)
async def test_create_rejections_are_distinct_from_uncertain_outcomes(mock_jira, monkeypatch, status, hint):
    """Report statuses where Jira refused the request as not created, without retrying."""
    requests, responses = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", "true")
    responses.extend([create_metadata(), httpx2.Response(status, text="test-secret")])

    result = await invoke("create_issue", {"project": "TEAM", "issue_type_id": "9", "summary": "Title"})

    assert result.is_error
    message = result.content[0].text
    assert message.startswith(f"No issue was created: Jira rejected the request with HTTP status {status}")
    assert hint in message
    assert "may already exist" not in message
    assert "test-secret" not in message
    assert [request.method for request in requests] == ["GET", "POST"]
