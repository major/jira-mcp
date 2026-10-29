"""Exercise server-wide behavior: discovery, schemas, instructions, and the entrypoint 🧪."""

import httpx2
import pytest
from fastmcp import Client

from jira_mcp import main, mcp
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


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


async def test_discovery_without_credentials(monkeypatch):
    """Let agents discover tools and get actionable configuration errors."""
    monkeypatch.delenv("JIRA_API_TOKEN")

    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool("list_fields", {}, raise_on_error=False)
        issue_result = await client.call_tool("list_fields", {"issue": "DEMO-1"}, raise_on_error=False)

    assert {tool.name for tool in tools} == {
        "create_issue",
        "get_issue",
        "get_issues",
        "get_issue_comments",
        "list_fields",
        "list_project_issue_types",
        "list_create_fields",
        "get_current_user",
        "search_users",
        "search_issues",
        "get_issue_transitions",
        "list_issue_link_types",
        "get_issue_remote_links",
        "transition_issue",
        "create_issue_link",
        "create_issue_remote_link",
    }
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


async def test_create_issue_schema():
    """Advertise bounded creation inputs."""
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    schema = tools["create_issue"].model_dump(by_alias=True)["inputSchema"]
    assert schema["required"] == ["project", "issue_type_id", "summary"]
    properties = schema["properties"]
    assert {"minLength": 1, "maxLength": 255}.items() <= properties["summary"].items()
    assert "pattern" in properties["summary"]
    assert {"maxLength": 100_000}.items() <= properties["description"]["anyOf"][0].items()
    assert {"maxItems": 100}.items() <= properties["labels"]["anyOf"][0].items()
    assert {"maxProperties": 100}.items() <= properties["additional_fields"]["anyOf"][0].items()


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
    assert "explicit user request" in instructions
    assert "before retrying" in instructions


def test_stdio_entrypoint(monkeypatch):
    """Launch the same registered server from the console entry point."""
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))

    main()

    assert calls == [{"transport": "stdio"}]


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
