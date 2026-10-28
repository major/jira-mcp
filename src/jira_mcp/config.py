"""Read and validate Jira Cloud connection settings."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Report a configuration error without exposing its value."""


@dataclass(frozen=True)
class Settings:
    """Connection settings for a Jira Cloud site or scoped-token gateway."""

    url: str
    email: str
    token: str = field(repr=False)
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        """Load required credentials and validate the Cloud URL and timeout."""
        required = ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
        values = {name: os.environ.get(name, "") for name in required}
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            msg = f"Missing required environment variables: {', '.join(missing)}"
            raise ConfigError(msg)

        url = _cloud_url(values["JIRA_URL"].strip())
        try:
            timeout = float(os.environ.get("JIRA_TIMEOUT", "30"))
        except ValueError:
            msg = "JIRA_TIMEOUT must be a finite positive number"
            raise ConfigError(msg) from None
        if not math.isfinite(timeout) or timeout <= 0:
            msg = "JIRA_TIMEOUT must be a finite positive number"
            raise ConfigError(msg)

        return cls(url=url, email=values["JIRA_EMAIL"].strip(), token=values["JIRA_API_TOKEN"], timeout=timeout)


def _cloud_url(value: str) -> str:
    """Accept only root Cloud sites and scoped-token gateway base URLs."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        msg = "JIRA_URL must be a valid Jira Cloud URL"
        raise ConfigError(msg) from None

    # Match the authority as well as hostname to reject user info, empty ports,
    # whitespace, and other ambiguous URL forms before credentials are used.
    site = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+atlassian\.net"
    authority = rf"(?:{site}|api\.atlassian\.com)(?::443)?"
    valid_authority = re.fullmatch(authority, parts.netloc, flags=re.IGNORECASE) is not None
    valid_site = re.fullmatch(site, parts.hostname or "", flags=re.IGNORECASE) is not None
    valid_gateway = (
        parts.hostname == "api.atlassian.com" and re.fullmatch(r"/ex/jira/[A-Za-z0-9-]+/?", parts.path) is not None
    )
    if (
        parts.scheme != "https"
        or not valid_authority
        or port not in (None, 443)
        or re.search(r"[\x00-\x20\x7f]", value) is not None
        or "?" in value
        or "#" in value
        or not ((valid_site and parts.path in ("", "/")) or valid_gateway)
    ):
        msg = "JIRA_URL must be a valid Jira Cloud URL"
        raise ConfigError(msg)
    return value.rstrip("/")
