"""Discover local server capabilities without contacting Jira."""

from fastmcp import Context  # noqa: TC002 - FastMCP resolves injected annotations at runtime.

from jira_mcp.app import get_client, mcp
from jira_mcp.common import READ_ONLY


@mcp.tool(annotations=READ_ONLY)
async def get_server_capabilities(ctx: Context) -> dict[str, bool]:
    """Return local write enablement without Jira requests.

    Flags reflect the startup snapshot until restart and do not affect other write
    tools. They do not establish Jira permissions or user authorization.
    """
    get_client(ctx)
    return {
        "issue_creation_enabled": ctx.lifespan_context["enable_issue_creation"],
        "issue_edits_enabled": ctx.lifespan_context["enable_issue_edits"],
    }
