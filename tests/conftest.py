"""Share Jira environment and HTTP boundary fixtures across tool tests 🧪."""

import httpx2
import pytest

from jira_mcp.client import JiraClient


@pytest.fixture
def jira_env(monkeypatch):
    """Provide deterministic Cloud credentials without using a real token."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-secret")
    monkeypatch.delenv("JIRA_TIMEOUT", raising=False)
    monkeypatch.delenv("JIRA_ENABLE_ISSUE_CREATION", raising=False)
    monkeypatch.delenv("JIRA_ATTACHMENT_DIRECTORIES", raising=False)
    monkeypatch.delenv("JIRA_ATTACHMENT_MAX_BYTES", raising=False)


@pytest.fixture
def mock_jira(monkeypatch):
    """Inject a native HTTPX2 transport while preserving the real client lifecycle."""
    requests = []
    responses = []
    original_init = JiraClient.__init__

    def handle(request):
        """Capture one request and return the next arranged response."""
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def initialize(self, settings):
        """Use the production initializer with a network-free transport."""
        original_init(self, settings, transport=httpx2.MockTransport(handle))

    monkeypatch.setattr(JiraClient, "__init__", initialize)
    return requests, responses
