"""Own the FastMCP server and shared Jira client lifecycle."""

from typing import Any
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan

from jira_mcp.client import JiraClient
from jira_mcp.config import ConfigError, Settings
from jira_mcp.fields import FieldCatalogCache
from jira_mcp.instructions import INSTRUCTIONS


@lifespan
async def jira_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Pool connections for a session while allowing discovery before configuration."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        yield {"client": None, "config_error": str(exc)}
        return
    client = JiraClient(settings)
    try:
        yield {
            "client": client,
            "field_catalog": FieldCatalogCache(client),
            "enable_issue_creation": settings.enable_issue_creation,
            "browse_base": settings.browse_base,
        }
    finally:
        await client.aclose()


mcp = FastMCP(
    name="jira-mcp",
    instructions=INSTRUCTIONS,
    lifespan=jira_lifespan,
    # Unexpected exceptions reach clients only as a generic message; ToolError text still passes through.
    mask_error_details=True,
)


def get_client(ctx: Context) -> JiraClient:
    """Get the pooled client or tell the caller which configuration is missing."""
    state = ctx.lifespan_context
    if state.get("client") is None:
        raise ToolError(state.get("config_error") or "Jira client is not configured.")
    return state["client"]


def get_field_catalog(ctx: Context) -> FieldCatalogCache:
    """Get the lifespan catalog cache, preserving client configuration errors."""
    get_client(ctx)
    return ctx.lifespan_context["field_catalog"]
