"""Check Cloud-only configuration without exposing credentials."""

import pytest

from jira_mcp.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate configuration tests from the invoking shell."""
    for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_and_secret_repr(monkeypatch):
    """Normalize the site URL and exclude the secret from repr."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "hidden-secret")

    settings = Settings.from_env()

    assert settings.url == "https://example.atlassian.net"
    assert settings.timeout == float("30")
    assert "hidden-secret" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.atlassian.net",
        "https://user:secret@example.atlassian.net",
        "https://example.atlassian.net/?secret=yes",
        "https://example.atlassian.net/#secret",
        "https://jira.example.com",
        "https://example.atlassian.net/rest/api/3",
        "https://api.atlassian.com/ex/jira",
        "https://api.atlassian.com/ex/jira/id/extra",
        "https://example.atlassian.net:8443",
        "https://[broken",
    ],
)
def test_invalid_cloud_urls(monkeypatch, url):
    """Reject unsupported URLs without repeating their contents."""
    monkeypatch.setenv("JIRA_URL", url)
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")

    with pytest.raises(ConfigError, match="JIRA_URL") as error:
        Settings.from_env()

    assert "secret" not in str(error.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "nope"])
def test_invalid_timeout(monkeypatch, timeout):
    """Accept only finite positive request timeouts."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    monkeypatch.setenv("JIRA_TIMEOUT", timeout)

    with pytest.raises(ConfigError, match="JIRA_TIMEOUT"):
        Settings.from_env()


def test_missing_settings():
    """Name required variables without leaking their values."""
    with pytest.raises(ConfigError, match=r"JIRA_URL.*JIRA_EMAIL.*JIRA_API_TOKEN"):
        Settings.from_env()
