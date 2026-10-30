"""Read and validate Jira Cloud connection settings."""

from __future__ import annotations

import json
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
    enable_issue_creation: bool = False
    attachment_directories: tuple[str, ...] = ()
    attachment_max_bytes: int = 10_485_760

    @property
    def browse_base(self) -> str | None:
        """Return the site URL for issue links, or None for the scoped-token gateway."""
        return None if urlsplit(self.url).hostname == "api.atlassian.com" else self.url

    @classmethod
    def from_env(cls) -> Settings:
        """Load credentials and validate the Cloud URL, timeout, and issue creation setting."""
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

        creation = os.environ.get("JIRA_ENABLE_ISSUE_CREATION", "false").lower()
        if creation not in ("true", "false"):
            msg = "JIRA_ENABLE_ISSUE_CREATION must be true or false"
            raise ConfigError(msg)

        try:
            roots = json.loads(os.environ.get("JIRA_ATTACHMENT_DIRECTORIES", "[]"))
            if not isinstance(roots, list) or any(
                not isinstance(root, str)
                or not root.startswith("/")
                or "\x00" in root
                or ".." in root.split("/")
                or not os.path.normpath(root).strip("/")
                for root in roots
            ):
                raise ValueError  # noqa: TRY301 - Share the safe configuration error below.
        except ValueError, TypeError:
            msg = "JIRA_ATTACHMENT_DIRECTORIES must be a JSON array of absolute directories"
            raise ConfigError(msg) from None

        try:
            limit = int(os.environ.get("JIRA_ATTACHMENT_MAX_BYTES", "10485760"))
            if not 0 < limit <= 104_857_600:  # noqa: PLR2004 - Maximum configurable per-file size is 100 MiB.
                raise ValueError  # noqa: TRY301 - Share the safe configuration error below.
        except ValueError:
            msg = "JIRA_ATTACHMENT_MAX_BYTES must be a positive integer at most 104857600"
            raise ConfigError(msg) from None

        return cls(
            url=url,
            email=values["JIRA_EMAIL"].strip(),
            token=values["JIRA_API_TOKEN"],
            timeout=timeout,
            enable_issue_creation=creation == "true",
            attachment_directories=tuple(roots),
            attachment_max_bytes=limit,
        )


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
