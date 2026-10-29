"""Call MCP tools through the real in-memory FastMCP client 🧪."""

from fastmcp import Client

from jira_mcp import mcp


async def invoke(name, arguments=None):
    """Call a registered tool through the MCP protocol and return errors as results."""
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments or {}, raise_on_error=False)
