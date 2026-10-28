"""Check Cloud-only configuration without exposing credentials."""

import pytest

from jira_mcp.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate configuration tests from the invoking shell."""
    for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_TIMEOUT", "JIRA_ENABLE_ISSUE_CREATION"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_and_secret_repr(monkeypatch):
    """Normalize the site URL and exclude the secret from repr."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "hidden-secret")

    settings = Settings.from_env()

    assert settings.url == "https://example.atlassian.net"
    assert settings.timeout == float("30")
    assert settings.enable_issue_creation is False
    assert "hidden-secret" not in repr(settings)


@pytest.fixture
def cloud_env(monkeypatch):
    """Provide valid Cloud credentials so each test changes only the setting under test."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "reader@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "hidden-secret")


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("false", False), ("FaLsE", False)])
def test_issue_creation_setting(cloud_env, monkeypatch, value, expected):
    """Accept only explicit case-insensitive boolean issue creation settings."""
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", value)

    settings = Settings.from_env()

    assert settings.enable_issue_creation is expected


@pytest.mark.parametrize("value", ["", "yes", "1", "test-secret", " true "])
def test_invalid_issue_creation_setting_is_safe(cloud_env, monkeypatch, value):
    """Reject ambiguous settings without echoing their values."""
    monkeypatch.setenv("JIRA_ENABLE_ISSUE_CREATION", value)

    with pytest.raises(ConfigError, match="JIRA_ENABLE_ISSUE_CREATION") as error:
        Settings.from_env()

    assert "test-secret" not in str(error.value)
    assert "hidden-secret" not in str(error.value)


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
