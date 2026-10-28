"""HTTP client for the Jira Cloud REST API."""

from typing import Any

import httpx2
from fastmcp.exceptions import ToolError

lazy from jira_mcp.config import Settings


class JiraClient:
    """Reuse a single authenticated HTTP client for Jira API requests."""

    def __init__(self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Configure authentication, timeouts, and redirect handling."""
        self._base_url = f"{settings.url.rstrip('/')}/rest/api/3"
        self._client = httpx2.AsyncClient(
            auth=(settings.email, settings.token),
            headers={"Accept": "application/json"},
            timeout=settings.timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Get and decode a Jira API response, hiding remote error details."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any], *, expect_json: bool = True) -> Any:
        """Send JSON, explicitly opting out of decoding for bodyless write responses."""
        return await self._request("POST", path, json=body, expect_json=expect_json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Decode an API response without exposing upstream or transport error details."""
        try:
            response = await self._client.request(
                method, f"{self._base_url}/{path.lstrip('/')}", params=params, json=json
            )
        except httpx2.RequestError:
            msg = "Jira request failed due to a network or timeout error"
            raise ToolError(msg) from None

        if not response.is_success:
            msg = f"Jira request failed with HTTP status {response.status_code}"
            raise ToolError(msg)
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError:
            msg = "Jira returned invalid JSON"
            raise ToolError(msg) from None

    async def aclose(self) -> None:
        """Close the pooled HTTP client."""
        await self._client.aclose()
