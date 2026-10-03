"""GitHub OAuth configuration and bounded, single-use browser login state."""

import secrets
import time
from urllib.parse import urlsplit

import jwt

from astrbot.core.config.provisioning import get_provisioning


def password_login_enabled() -> bool:
    """Return whether local dashboard authentication is enabled."""
    return get_provisioning().auth.password_login_enabled


def validate_login_source(payload: dict) -> None:
    """Reject existing local sessions when password login is disabled.

    Args:
        payload: Verified JWT claims.

    Raises:
        jwt.InvalidTokenError: The session uses disabled local authentication.
    """
    if not password_login_enabled() and payload.get("auth_source") != "github":
        raise jwt.InvalidTokenError("Local authentication is disabled")


class GitHubOAuth:
    def __init__(self) -> None:
        config = get_provisioning().auth.github
        self.enabled = config is not None
        self.client_id = config.client_id if config else ""
        self.client_secret = config.client_secret if config else ""
        self.redirect_uri = config.redirect_uri if config else ""
        self.organizations = (
            {org.strip().lower() for org in config.allowed_organizations if org.strip()}
            if config
            else set()
        )
        self.users = (
            {user.strip().lower() for user in config.allowed_users if user.strip()}
            if config
            else set()
        )
        self.secure_cookie = urlsplit(self.redirect_uri).scheme == "https"
        prefix = "__Host-" if self.secure_cookie else ""
        self.state_cookie = prefix + "astrbot_oauth_state"
        self.ticket_cookie = prefix + "astrbot_oauth_ticket"
        self.pending: dict[str, tuple[float, str]] = {}

    def issue(self, value: str) -> str:
        """Store a short-lived verifier or completed identity under a random key.

        Args:
            value: PKCE verifier or GitHub user ID.

        Returns:
            A cryptographically random, single-use key.

        Raises:
            ValueError: Too many pending logins are outstanding.
        """
        now = time.monotonic()
        self.pending = {
            key: item for key, item in self.pending.items() if item[0] > now
        }
        if len(self.pending) >= 1024:
            raise ValueError("Too many pending OAuth logins")
        key = secrets.token_urlsafe(32)
        self.pending[key] = (now + 300, value)
        return key

    def consume(self, key: str) -> str | None:
        """Consume unexpired login state once.

        Args:
            key: Browser-bound state or session ticket.

        Returns:
            Stored value, or None for expired, missing or replayed state.
        """
        item = self.pending.pop(key, None)
        return item[1] if item and item[0] > time.monotonic() else None
