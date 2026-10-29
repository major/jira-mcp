"""Validate and cache the visible Jira field catalog for one server lifespan."""

from asyncio import Lock
from time import monotonic
from typing import Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, RootModel, ValidationError

from jira_mcp.common import INVALID_PAYLOAD
lazy from jira_mcp.client import JiraClient

CATALOG_TTL = 300


class FieldDefinition(BaseModel):
    """Keep field IDs stable and custom field types open-ended."""

    id: str
    name: str
    custom: bool = False
    field_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class FieldCatalog(RootModel[list[FieldDefinition]]):
    """Validate the unpaged field catalog returned by Jira Cloud."""


class FieldCatalogCache:
    """Share validated catalog results for five minutes within one lifespan."""

    def __init__(self, client: JiraClient) -> None:
        """Associate an initially empty cache and lock with the pooled client."""
        self._client = client
        self._catalog: list[FieldDefinition] | None = None
        self._expires_at = 0.0
        self._lock = Lock()

    async def get(self, refresh: bool = False) -> list[FieldDefinition]:
        """Return a fresh catalog, coalescing callers that wait for a successful fetch."""
        observed = self._catalog
        if not refresh and observed is not None and monotonic() < self._expires_at:
            return observed

        async with self._lock:
            # A caller that arrived while another fetch was running can reuse its result,
            # including when both requested an explicit refresh.
            if self._catalog is not None and self._catalog is not observed and monotonic() < self._expires_at:
                return self._catalog
            data = await self._client.get("field")
            try:
                catalog = FieldCatalog.model_validate(data).root
            except ValidationError:
                raise ToolError(INVALID_PAYLOAD) from None
            self._catalog = catalog
            self._expires_at = monotonic() + CATALOG_TTL
            return catalog
