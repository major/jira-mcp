"""Characterize JSON reads and explicit bodyless Jira writes."""

import httpx2
import pytest
from fastmcp.exceptions import ToolError

from jira_mcp.client import JiraClient
from jira_mcp.config import Settings


@pytest.mark.parametrize("status", [201, 204])
async def test_explicit_bodyless_post(status):
    """Accept Jira's empty write acknowledgments without attempting JSON decoding."""
    requests = []

    def handle(request):
        """Record the request and acknowledge the write without a body."""
        requests.append(request)
        return httpx2.Response(status)

    client = JiraClient(
        Settings(url="https://example.atlassian.net", email="test@example.com", token="test-secret"),  # noqa: S106
        transport=httpx2.MockTransport(handle),
    )

    try:
        result = await client.post("issueLink", {"type": {"id": "1"}}, expect_json=False)
    finally:
        await client.aclose()

    assert result is None
    assert len(requests) == 1
    assert requests[0].method == "POST"


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [200, 201, 204])
async def test_json_requests_still_require_json(method, status):
    """Keep default JSON reads strict even when the HTTP status indicates success."""

    def handle(_request):
        """Return an empty response that cannot satisfy the JSON contract."""
        return httpx2.Response(status)

    client = JiraClient(
        Settings(url="https://example.atlassian.net", email="test@example.com", token="test-secret"),  # noqa: S106
        transport=httpx2.MockTransport(handle),
    )

    try:
        request = client.get("myself") if method == "GET" else client.post("search/jql", {})
        with pytest.raises(ToolError, match="Jira returned invalid JSON"):
            await request
    finally:
        await client.aclose()
