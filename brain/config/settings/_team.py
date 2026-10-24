"""Team-mode and OIDC fields."""
from __future__ import annotations

from typing import Optional
from pydantic import field_validator


class SettingsTeam:
    # Deprecated strictness alias: a Brain is a team of one until its first member joins;
    # True treats an empty team as a team (shared key and keyless access refused).
    BRAIN_TEAM_MODE: bool = False

    # Generic OpenID Connect single sign-on for team members
    # (brain/team/oidc.py, docs/operations/TEAM_SSO.md). Off by default. The
    # issuer must publish /.well-known/openid-configuration; the redirect URI is
    # the web UI callback and defaults to BRAIN_WEB_URL + /auth/oidc/callback.
    BRAIN_OIDC_ENABLED: bool = False

    BRAIN_OIDC_ISSUER: Optional[str] = None

    BRAIN_OIDC_CLIENT_ID: Optional[str] = None

    BRAIN_OIDC_CLIENT_SECRET: Optional[str] = None

    BRAIN_OIDC_REDIRECT_URI: Optional[str] = None

    BRAIN_OIDC_SCOPES: str = "openid email profile"

    # Comma-separated email domains allowed to use SSO; empty allows any domain.
    BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS: str = ""

    # With SSO on: refuse token-only invitation acceptance (and invitations
    # without an email), so every new member proves the invited address.
    BRAIN_OIDC_REQUIRE_FOR_INVITATIONS: bool = False

    # Team transactions (team, invitation and coordination routes) wait at most
    # this long for a row lock before answering HTTP 503 with Retry-After, and a
    # team transaction left idle this long is ended by PostgreSQL, so one stuck
    # request cannot hold team rows (and pool connections) for minutes.
    # 0 turns a bound off. See brain/team/locking.py.
    BRAIN_TEAM_LOCK_TIMEOUT_MS: int = 5000

    BRAIN_TEAM_IDLE_TX_TIMEOUT_MS: int = 30000

    # Opt-in: in team state, members may link their own chat on /team/me (the bot above sends a one-time
    # code) and get review-needed notices there. Independent of the owner channel switch above.
    TEAM_MEMBER_TELEGRAM_NOTIFY: bool = False

    @field_validator("BRAIN_TEAM_LOCK_TIMEOUT_MS", "BRAIN_TEAM_IDLE_TX_TIMEOUT_MS")
    @classmethod
    def validate_team_transaction_bounds(cls, value: int) -> int:
        if not 0 <= value <= 600_000:
            raise ValueError("Team transaction timeouts must be between 0 and 600000 milliseconds")
        return value
