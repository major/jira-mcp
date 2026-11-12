"""Discover local write enablement without Jira requests or configuration guesses."""

import pytest
from fastmcp import Client

from jira_mcp import mcp
from tests.helpers import invoke

pytestmark = pytest.mark.usefixtures("jira_env")


@pytest.mark.parametrize("creation_enabled", [False, True])
@pytest.mark.parametrize("edits_enabled", [False, True])
async def test_capabilities_report_startup_settings_without_requests(
    mock_jira, monkeypatch, creation_enabled, edits_enabled
):
    """Return only the enforced startup flags, even if the environment changes later."""
    # Arrange
    requests, _ = mock_jira
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", str(creation_enabled))
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_EDITS", str(edits_enabled))

    # Act
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        first = await client.call_tool("get_server_capabilities", {}, raise_on_error=False)
        monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", str(not creation_enabled))
        monkeypatch.setenv("JIRA_ENABLE_ISSUE_EDITS", str(not edits_enabled))
        second = await client.call_tool("get_server_capabilities", {}, raise_on_error=False)

    # Assert
    assert not first.is_error
    assert first.structured_content == {
        "issue_creation_enabled": creation_enabled,
        "issue_edits_enabled": edits_enabled,
    }
    assert not second.is_error
    assert second.structured_content == first.structured_content
    assert not tools["get_server_capabilities"].input_schema.get("required")
    assert tools["get_server_capabilities"].input_schema["properties"] == {}
    assert requests == []


@pytest.mark.parametrize("variable", ["JIRA_API_TOKEN", "JIRA_ENABLE_ISSUE_CREATION", "JIRA_ENABLE_ISSUE_EDITS"])
async def test_capabilities_report_configuration_errors(mock_jira, monkeypatch, variable):
    """Report invalid configuration instead of inventing disabled capabilities."""
    # Arrange
    requests, _ = mock_jira
    if variable == "JIRA_API_TOKEN":
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, "test-secret")

    # Act
    result = await invoke("get_server_capabilities")

    # Assert
    assert result.is_error
    assert variable in result.content[0].text
    assert "test-secret" not in result.content[0].text
    assert result.structured_content is None
    assert requests == []
