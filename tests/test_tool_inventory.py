"""Keep registered tools, annotations, README, and server instructions in sync 📋."""

import re
from pathlib import Path

import pytest
from fastmcp import Client

from jira_mcp import mcp

# Every public tool and the kind of change it can make in Jira.
TOOLS = {
    "list_issue_attachments": "read",
    "get_attachment": "read",
    "download_issue_attachments": "write",
    "add_issue_attachments": "create",
    "delete_attachment": "write",
    "get_current_user": "read",
    "search_users": "read",
    "get_issue": "read",
    "get_issues": "read",
    "search_issues": "read",
    "get_issue_comments": "read",
    "add_issue_comment": "create",
    "list_fields": "read",
    "list_project_issue_types": "read",
    "list_create_fields": "read",
    "create_issue": "create",
    "get_issue_transitions": "read",
    "transition_issue": "write",
    "assign_issue": "write",
    "list_issue_link_types": "read",
    "create_issue_link": "write",
    "get_issue_remote_links": "read",
    "create_issue_remote_link": "write",
}
# Expected (readOnly, destructive, idempotent, openWorld) hints for each kind.
ANNOTATIONS = {
    "read": (True, False, True, True),
    "create": (False, False, False, True),
    "write": (False, True, False, True),
}


@pytest.fixture
async def tools():
    """List the registered tools by name through the real MCP client."""
    async with Client(mcp) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def test_registered_tools_match_inventory_and_annotations(tools):
    """Register exactly the inventoried tools with truthful annotations."""
    # Arrange
    expected = {name: ANNOTATIONS[kind] for name, kind in TOOLS.items()}

    # Act
    actual = {
        name: (
            tool.annotations.read_only_hint,
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
            tool.annotations.open_world_hint,
        )
        for name, tool in tools.items()
    }

    # Assert
    assert actual == expected


def test_readme_lists_every_tool_and_marks_writes():
    """Keep one README row per tool and mark exactly the writes."""
    # Arrange
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    table = readme.split("## Tools", 1)[1].split("\n\n<details>", 1)[0]

    # Act
    rows = dict(re.findall(r"^\| `(\w+)` \| (.*) \|$", table, flags=re.MULTILINE))

    # Assert
    assert {name: "**Writes.**" in purpose for name, purpose in rows.items()} == {
        name: kind != "read" for name, kind in TOOLS.items()
    }


async def test_instructions_list_every_tool_with_its_arguments(tools):
    """Name each tool's arguments in the instructions so one tool search is enough."""
    # Arrange
    instructions = mcp.instructions or ""
    main_arguments = instructions.split("Main arguments:", 1)[1]

    # Act
    listed = {
        name: [arg for arg in args.split(", ") if arg] for name, args in re.findall(r"(\w+)\(([^)]*)\)", main_arguments)
    }

    # Assert
    assert listed == {name: list(tool.input_schema["properties"]) for name, tool in tools.items()}
