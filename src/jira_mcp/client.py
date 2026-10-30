"""HTTP client for the Jira Cloud REST API."""

from http import HTTPStatus
from typing import Any, BinaryIO

import httpx2
from fastmcp.exceptions import ToolError

lazy from jira_mcp.config import Settings


class JiraHTTPError(ToolError):
    """Report an unsuccessful Jira status code so writes can classify the outcome."""

    def __init__(self, status_code: int) -> None:
        """Keep the status code and a safe message without the upstream body."""
        super().__init__(f"Jira request failed with HTTP status {status_code}")
        self.status_code = status_code


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

    async def put(self, path: str, body: dict[str, Any]) -> None:
        """Send JSON to a bodyless update endpoint such as an issue's assignee."""
        await self._request("PUT", path, json=body, expect_json=False)

    async def upload_attachment(self, issue: str, filename: str, content: bytes | BinaryIO) -> Any:
        """Upload one multipart attachment without changing pooled headers."""
        return await self._request(
            "POST",
            f"issue/{issue}/attachments",
            files={"file": (filename, content, "application/octet-stream")},
            headers={"X-Atlassian-Token": "no-check"},
        )

    async def delete_attachment(self, attachment_id: str) -> None:
        """Delete an attachment only accepting Jira's documented 204 response."""
        await self._request("DELETE", f"attachment/{attachment_id}", expect_json=False, expected_status=204)

    async def download_attachment(self, attachment_id: str, max_bytes: int) -> bytes:
        """Read bounded raw attachment bytes without redirects or decompression."""
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/attachment/content/{attachment_id}",
                params={"redirect": "false"},
                headers={"Accept": "*/*", "Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code != HTTPStatus.OK:
                    raise JiraHTTPError(response.status_code)
                if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
                    msg = "Jira returned an encoded attachment response"
                    raise ToolError(msg)
                content = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        msg = "Attachment exceeds the requested byte limit"
                        raise ToolError(msg)
                return bytes(content)
        except httpx2.RequestError:
            msg = "Jira request failed due to a network or timeout error"
            raise ToolError(msg) from None

    async def _request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expect_json: bool = True,
        files: Any = None,
        headers: dict[str, str] | None = None,
        expected_status: int | None = None,
    ) -> Any:
        """Decode an API response without exposing upstream or transport error details."""
        try:
            response = await self._client.request(
                method, f"{self._base_url}/{path.lstrip('/')}", params=params, json=json, files=files, headers=headers
            )
        except httpx2.RequestError:
            msg = "Jira request failed due to a network or timeout error"
            raise ToolError(msg) from None

        if not response.is_success or (expected_status is not None and response.status_code != expected_status):
            raise JiraHTTPError(response.status_code)
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
