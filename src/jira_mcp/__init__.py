"""Jira Cloud MCP server with concise reads, writes, and opt-in issue creation ☁️."""

from jira_mcp import tools  # noqa: F401 - Importing the package registers every tool.
from jira_mcp.app import mcp

__all__ = ["main", "mcp"]


def main() -> None:
    """Run the registered tools over stdio without writing to protocol stdout."""
    mcp.run(transport="stdio")
