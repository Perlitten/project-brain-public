"""Generic OpenID Connect single sign-on for team members.

Off unless ``BRAIN_OIDC_ENABLED``. One identity provider per Brain, found by
discovery (``<issuer>/.well-known/openid-configuration``). The dashboard
(apps/web ``/auth/oidc/*``) drives the browser; this module holds the
protocol and the authorization decisions:

* ``begin`` creates a single-use, ten-minute authorization request
  (authorization-code flow with PKCE S256, ``state`` and ``nonce``) and returns
  the provider URL. Only hashes of ``state`` and of a browser binding secret
  are stored; the dashboard keeps both in an httpOnly cookie, so an
  authorization code is useless to anyone who lacks that browser's cookie.
* ``consume_flow`` spends the request (its own transaction, before any
  network call), ``authenticate`` exchanges the code with the stored PKCE
  verifier and validates the ID token: signature against the provider's JWKS
  (asymmetric algorithms only), ``iss``, ``aud`` (and ``azp`` when several
  audiences), ``exp``/``iat``, ``nonce``, ``email_verified`` true and the
  optional allowed email domains.
* ``sign_in`` maps the validated issuer + subject to a bound team member
  (``TeamIdentity``) and mints a seven-day dashboard credential with exactly
  the scopes of that member's dashboard key. Unknown subjects, revoked or
  disabled members and a fenced (restored) database are refused; there is no
  auto-provisioning.
* Invitation acceptance through SSO is ``invitations.accept_invitation_oidc``.

Library: PyJWT (``pyjwt[crypto]``, already locked as an MCP dependency and now
declared directly) verifies signatures and registered claims; httpx fetches
discovery, JWKS and the token endpoint. No OIDC-specific client library is
added: the flow needs four HTTP calls, and keeping them here keeps every
check visible and testable against a fake provider.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit

import httpx
import jwt
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import mint_credential
from brain.config.settings import settings
from brain.database.models import Principal
from brain.team import invitations
from brain.team.authorization import TeamConflict, TeamDenied, fresh
from brain.team.models import TeamIdentity, TeamMember, TeamOidcFlow
from brain.team.provisioning import TEAM_ID, TeamNotFound, _iso, _now, member_dict, member_scopes
from brain.team.restore_fence import FENCED, access_fenced

FLOW_TTL = timedelta(minutes=10)
SESSION_TTL_DAYS = 7  # matches the dashboard's member cookie (apps/web MEMBER_MAX_AGE)
METADATA_TTL_S = 3600
JWKS_TTL_S = 3600
JWKS_REFRESH_MIN_S = 30
CLOCK_SKEW_S = 60
HTTP_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1_000_000
MIN_BINDING_LENGTH = 32
MAX_BINDING_LENGTH = 128
# Never HS* (the client secret is not a signing key here) and never "none".
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DISABLED = "Single sign-on is not enabled on this Brain"
FLOW_REJECTED = "The sign-in request is invalid, expired or already used; start again"
TOKEN_REJECTED = "The identity provider's response could not be verified"
NOT_LINKED = "No active team member is linked to this single sign-on account"

# Tests inject an ``httpx.MockTransport``; production uses the default transport.
http_transport: httpx.AsyncBaseTransport | None = None


class OidcDisabled(LookupError):
    """SSO is switched off (the router answers 404)."""


class OidcRejected(TeamDenied):
    """The flow, the provider's answer or the identity was refused (403)."""


class OidcUnavailable(RuntimeError):
    """The identity provider could not be reached or answered nonsense (502)."""


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scopes: str
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    require_for_invitations: bool = False


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: tuple[str, ...]
    token_auth_methods: tuple[str, ...]


@dataclass(frozen=True)
class FlowTicket:
    purpose: str
    nonce: str
    code_verifier: str
    invitation_id: uuid.UUID | None


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    email: str
    name: str | None = None


_metadata_cache: dict[str, tuple[float, ProviderMetadata]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def reset_caches() -> None:
    _metadata_cache.clear()
    _jwks_cache.clear()


def oidc_config() -> OidcConfig | None:
    """The configured provider, or None when SSO is off (settings validate it at startup)."""
    if not settings.BRAIN_OIDC_ENABLED:
        return None
    issuer = (settings.BRAIN_OIDC_ISSUER or "").strip().rstrip("/")
    client_id = (settings.BRAIN_OIDC_CLIENT_ID or "").strip()
    redirect = (settings.BRAIN_OIDC_REDIRECT_URI or "").strip()
    if not redirect and (settings.BRAIN_WEB_URL or "").strip():
        redirect = (settings.BRAIN_WEB_URL or "").strip().rstrip("/") + "/auth/oidc/callback"
    if not issuer or not client_id or not redirect:
        return None
    domains = frozenset(item.strip().lower().lstrip("@") for item in settings.BRAIN_OIDC_ALLOWED_EMAIL_DOMAINS.split(",")
                        if item.strip())
    return OidcConfig(issuer=issuer, client_id=client_id, client_secret=(settings.BRAIN_OIDC_CLIENT_SECRET or None),
                      redirect_uri=redirect, scopes=settings.BRAIN_OIDC_SCOPES.strip() or "openid email",
                      allowed_domains=domains, require_for_invitations=bool(settings.BRAIN_OIDC_REQUIRE_FOR_INVITATIONS))


def require_config() -> OidcConfig:
    config = oidc_config()
    if config is None:
        raise OidcDisabled(DISABLED)
    return config


def public_status() -> dict[str, bool]:
    """What the sign-in page may know: whether to offer SSO and whether invitations need it."""
    config = oidc_config()
    return {"enabled": config is not None,
            "require_for_invitations": bool(config and config.require_for_invitations)}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """RFC 7636 verifier (86 chars of base64url) and its S256 challenge."""
    verifier = _b64url(secrets.token_bytes(64))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _check_endpoint(url: object, issuer: str, name: str) -> str:
    if not isinstance(url, str) or not url:
        raise OidcUnavailable(f"The provider's discovery document has no {name}")
    parsed = urlparse(url)
    issuer_host = (urlparse(issuer).hostname or "").lower()
    loopback = issuer_host in LOOPBACK_HOSTS and (parsed.hostname or "").lower() in LOOPBACK_HOSTS
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise OidcUnavailable(f"The provider's {name} is not a usable URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise OidcUnavailable(f"The provider's {name} must use https")
    return url


def _client(auth: httpx.Auth | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=http_transport, timeout=HTTP_TIMEOUT_S, follow_redirects=False, auth=auth)


def _json(response: httpx.Response, what: str) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise OidcUnavailable(f"The provider's {what} is too large")
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise OidcUnavailable(f"The provider's {what} is not JSON") from exc
    if not isinstance(data, dict):
        raise OidcUnavailable(f"The provider's {what} is not a JSON object")
    return data


async def _get_json(url: str, what: str) -> dict[str, Any]:
    try:
        async with _client() as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OidcUnavailable(f"The identity provider did not answer ({what})") from exc
    if response.status_code != 200:
        raise OidcUnavailable(f"The identity provider answered HTTP {response.status_code} ({what})")
    return _json(response, what)


async def discover(config: OidcConfig) -> ProviderMetadata:
    cached = _metadata_cache.get(config.issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    data = await _get_json(f"{config.issuer}/.well-known/openid-configuration", "discovery document")
    # OpenID Connect Discovery 1.0 §4.3: the issuer must match exactly.
    if data.get("issuer") != config.issuer and str(data.get("issuer", "")).rstrip("/") != config.issuer:
        raise OidcUnavailable("The provider's discovery document names a different issuer")
    advertised = data.get("id_token_signing_alg_values_supported") or ["RS256"]
    algorithms = tuple(alg for alg in advertised if alg in ALLOWED_ALGORITHMS) if isinstance(advertised, list) else ()
    if not algorithms:
        raise OidcUnavailable("The provider signs ID tokens with no algorithm this Brain accepts")
    methods = data.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    metadata = ProviderMetadata(
        issuer=str(data["issuer"]),
        authorization_endpoint=_check_endpoint(data.get("authorization_endpoint"), config.issuer,
                                               "authorization_endpoint"),
        token_endpoint=_check_endpoint(data.get("token_endpoint"), config.issuer, "token_endpoint"),
        jwks_uri=_check_endpoint(data.get("jwks_uri"), config.issuer, "jwks_uri"),
        algorithms=algorithms,
        token_auth_methods=tuple(str(item) for item in methods) if isinstance(methods, list) else (),
    )
    _metadata_cache[config.issuer] = (time.monotonic() + METADATA_TTL_S, metadata)
    return metadata


async def _jwks(metadata: ProviderMetadata, *, refresh: bool = False) -> dict[str, Any]:
    cached = _jwks_cache.get(metadata.jwks_uri)
    now = time.monotonic()
    if cached and cached[0] > now and not (refresh and cached[0] - JWKS_TTL_S + JWKS_REFRESH_MIN_S <= now):
        return cached[1]
    data = await _get_json(metadata.jwks_uri, "key set")
    if not isinstance(data.get("keys"), list):
        raise OidcUnavailable("The provider's key set has no keys")
    _jwks_cache[metadata.jwks_uri] = (now + JWKS_TTL_S, data)
    return data


def _pick_key(jwks: dict[str, Any], kid: str | None, alg: str) -> jwt.PyJWK | None:
    candidates = []
    for entry in jwks.get("keys", []):
        if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
            continue
        if kid is not None and entry.get("kid") != kid:
            continue
        if entry.get("alg") not in (None, alg):
            continue
        try:
            candidates.append(jwt.PyJWK(entry, algorithm=alg))
        except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
            continue
    # Without a kid only an unambiguous key set is acceptable.
    return candidates[0] if len(candidates) == 1 or (kid is not None and candidates) else None


async def verify_id_token(config: OidcConfig, metadata: ProviderMetadata, id_token: str, nonce: str) -> OidcIdentity:
    """Validate an ID token and return the identity it vouches for (raises ``OidcRejected``)."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcRejected(TOKEN_REJECTED) from exc
    alg = header.get("alg")
    if alg not in metadata.algorithms:
        raise OidcRejected(TOKEN_REJECTED)
    kid = header.get("kid") if isinstance(header.get("kid"), str) else None
    key = _pick_key(await _jwks(metadata), kid, alg)
    if key is None:  # the provider may have rotated its keys since we cached them
        key = _pick_key(await _jwks(metadata, refresh=True), kid, alg)
    if key is None:
        raise OidcRejected(TOKEN_REJECTED)
    try:
        claims = jwt.decode(id_token, key=key.key, algorithms=[alg], audience=config.client_id,
                            issuer=metadata.issuer, leeway=CLOCK_SKEW_S,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise OidcRejected(TOKEN_REJECTED) from exc
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != config.client_id:
        raise OidcRejected(TOKEN_REJECTED)
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce.encode(), nonce.encode()):
        raise OidcRejected(TOKEN_REJECTED)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
        raise OidcRejected(TOKEN_REJECTED)
    email = claims.get("email")
    if not isinstance(email, str) or not invitations.EMAIL_PATTERN.fullmatch(email.strip()):
        raise OidcRejected("The identity provider did not return an email address; request the email scope")
    verified = claims.get("email_verified")
    if verified is not True and verified != "true":
        raise OidcRejected("The identity provider has not verified this account's email address")
    email = email.strip()
    if config.allowed_domains and email.rsplit("@", 1)[1].lower() not in config.allowed_domains:
        raise OidcRejected("This email domain may not use single sign-on here")
    name = claims.get("name")
    # Bindings are stored under the configured issuer (trailing slash dropped), so
    # operator-made bindings and SSO-accepted members use the same string.
    return OidcIdentity(issuer=config.issuer, subject=subject, email=email,
                        name=name if isinstance(name, str) else None)


async def _exchange_code(config: OidcConfig, metadata: ProviderMetadata, code: str, verifier: str) -> str:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": config.redirect_uri,
            "code_verifier": verifier, "client_id": config.client_id}
    auth = None
    if config.client_secret:
        if "client_secret_basic" in metadata.token_auth_methods or "client_secret_post" not in metadata.token_auth_methods:
            # RFC 6749 §2.3.1: form-urlencode both parts before HTTP Basic.
            auth = httpx.BasicAuth(urlencode({"": config.client_id})[1:], urlencode({"": config.client_secret})[1:])
        else:
            form["client_secret"] = config.client_secret
    try:
        async with _client(auth) as client:
            response = await client.post(metadata.token_endpoint, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OidcUnavailable("The identity provider did not answer (token endpoint)") from exc
    if response.status_code in (400, 401):
        raise OidcRejected("The identity provider refused the authorization code; start again")
    if response.status_code != 200:
        raise OidcUnavailable(f"The identity provider answered HTTP {response.status_code} (token endpoint)")
    id_token = _json(response, "token response").get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OidcRejected(TOKEN_REJECTED)
    return id_token


async def begin(session: AsyncSession, *, binding: str, invitation_token: str | None = None) -> dict[str, Any]:
    """Create an authorization request; returns the provider URL to send the browser to."""
    config = require_config()
    if not MIN_BINDING_LENGTH <= len(binding or "") <= MAX_BINDING_LENGTH:
        raise OidcRejected("A browser binding of 32-128 characters is required")
    if await access_fenced(session):
        raise TeamConflict(FENCED)
    invitation = None
    if invitation_token:
        invitation = await invitations.pending_invitation(session, invitation_token)
        if not invitation.email:
            raise TeamConflict("This invitation names no email address, so single sign-on cannot verify it; "
                               "ask an administrator for an invitation with your email")
    metadata = await discover(config)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    now = _now()
    await session.execute(delete(TeamOidcFlow).where(or_(TeamOidcFlow.expires_at < now - timedelta(days=1),
                                                         TeamOidcFlow.consumed_at < now - timedelta(days=1))))
    session.add(TeamOidcFlow(state_hash=_digest(state), binding_hash=_digest(binding), nonce=nonce,
                             code_verifier=verifier, purpose="invitation" if invitation else "signin",
                             invitation_id=invitation.id if invitation else None, created_at=now,
                             expires_at=now + FLOW_TTL))
    await session.flush()
    params = {"response_type": "code", "client_id": config.client_id, "redirect_uri": config.redirect_uri,
              "scope": config.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
              "code_challenge_method": "S256"}
    if invitation is not None and invitation.email:
        params["login_hint"] = invitation.email
    parts = urlsplit(metadata.authorization_endpoint)
    query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
    return {"authorization_url": urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")),
            "state": state, "purpose": "invitation" if invitation else "signin",
            "expires_at": _iso(now + FLOW_TTL)}


async def consume_flow(session: AsyncSession, *, state: str, binding: str) -> FlowTicket:
    """Spend an authorization request once; run in its own transaction before the code exchange."""
    require_config()
    if not state or len(state) > 256 or not binding or len(binding) > MAX_BINDING_LENGTH:
        raise OidcRejected(FLOW_REJECTED)
    flow = await fresh(session, TeamOidcFlow, TeamOidcFlow.state_hash == _digest(state), exclusive=True)
    if (flow is None or flow.consumed_at is not None or flow.expires_at <= _now()
            or not hmac.compare_digest(flow.binding_hash, _digest(binding))):
        raise OidcRejected(FLOW_REJECTED)
    flow.consumed_at = _now()
    await session.flush()
    return FlowTicket(purpose=flow.purpose, nonce=flow.nonce, code_verifier=flow.code_verifier,
                      invitation_id=flow.invitation_id)


async def authenticate(ticket: FlowTicket, code: str) -> OidcIdentity:
    """Exchange the code (with the PKCE verifier) and validate the ID token. No database access."""
    config = require_config()
    if not code or len(code) > 2048:
        raise OidcRejected(FLOW_REJECTED)
    metadata = await discover(config)
    id_token = await _exchange_code(config, metadata, code, ticket.code_verifier)
    return await verify_id_token(config, metadata, id_token, ticket.nonce)


async def sign_in(session: AsyncSession, identity: OidcIdentity, team_id: int = TEAM_ID) -> dict[str, Any]:
    """Mint a dashboard credential for the live member bound to ``identity``."""
    if await access_fenced(session):
        raise TeamConflict(FENCED)
    binding = await fresh(session, TeamIdentity, TeamIdentity.issuer == identity.issuer,
                          TeamIdentity.subject == identity.subject, TeamIdentity.team_id == team_id, exclusive=True)
    if binding is None:
        raise OidcRejected(NOT_LINKED)
    member = await fresh(session, TeamMember, TeamMember.principal_id == binding.principal_id,
                         TeamMember.team_id == team_id)
    principal = await fresh(session, Principal, Principal.id == binding.principal_id)
    if (member is None or member.revoked_at is not None or not member.verified_at or principal is None
            or principal.kind != "human" or principal.disabled_at is not None):
        raise OidcRejected(NOT_LINKED)
    raw, credential = await mint_credential(session, name=principal.name, scopes=member_scopes(bool(member.is_admin)),
                                            kind="human", ttl_days=SESSION_TTL_DAYS)
    binding.last_login_at = _now()
    binding.email = identity.email
    await session.flush()
    return {"mode": "signin", "credential": raw, "expires_at": _iso(credential.expires_at),
            "member": member_dict(member, principal)}


async def accept(session: AsyncSession, ticket: FlowTicket, identity: OidcIdentity) -> dict[str, Any]:
    """Accept the flow's invitation for ``identity``; a repeat by the same identity signs in instead."""
    if ticket.invitation_id is None:
        raise OidcRejected(FLOW_REJECTED)
    result = await invitations.accept_invitation_oidc(session, ticket.invitation_id, issuer=identity.issuer,
                                                      subject=identity.subject, email=identity.email,
                                                      ttl_days=SESSION_TTL_DAYS)
    if result is None:
        return await sign_in(session, identity)
    return {"mode": "invitation", "credential": result["credential"], "member": result["member"],
            "invitation": result["invitation"], "project_memberships": result["project_memberships"]}


def identity_dict(row: TeamIdentity, principal_name: str | None = None) -> dict[str, Any]:
    return {"id": row.id, "principal_id": row.principal_id, "principal_name": principal_name,
            "issuer": row.issuer, "subject": row.subject, "email": row.email,
            "invitation_id": str(row.invitation_id) if row.invitation_id else None, "bound_by": row.bound_by,
            "created_at": _iso(row.created_at), "last_login_at": _iso(row.last_login_at)}


async def list_identities(session: AsyncSession, team_id: int = TEAM_ID) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(TeamIdentity, Principal.name).join(Principal, Principal.id == TeamIdentity.principal_id)
        .where(TeamIdentity.team_id == team_id).order_by(TeamIdentity.issuer, TeamIdentity.subject))).all()
    return [identity_dict(row, name) for row, name in rows]


async def bind_identity(session: AsyncSession, *, principal_name: str, subject: str, issuer: str | None = None,
                        email: str | None = None, bound_by: str, team_id: int = TEAM_ID) -> dict[str, Any]:
    """Operator action: let an existing member sign in with an SSO account (no email check)."""
    issuer = (issuer or settings.BRAIN_OIDC_ISSUER or "").strip().rstrip("/")
    subject = (subject or "").strip()
    if not issuer or not subject or len(subject) > 255:
        raise TeamConflict("An issuer (or BRAIN_OIDC_ISSUER) and a subject of at most 255 characters are required")
    principal = await fresh(session, Principal, Principal.name == principal_name)
    if principal is None:
        raise TeamNotFound(f"Principal {principal_name!r} not found")
    member = await fresh(session, TeamMember, TeamMember.principal_id == principal.id, TeamMember.team_id == team_id)
    if member is None or member.revoked_at is not None or not member.verified_at:
        raise TeamConflict(f"{principal_name!r} is not an active team member")
    claimed = await fresh(session, TeamIdentity, TeamIdentity.issuer == issuer, TeamIdentity.subject == subject)
    if claimed is not None:
        if claimed.principal_id != principal.id:
            raise TeamConflict("This SSO identity is already bound to another member")
        return identity_dict(claimed, principal.name)
    if await fresh(session, TeamIdentity, TeamIdentity.principal_id == principal.id,
                   TeamIdentity.issuer == issuer) is not None:
        raise TeamConflict(f"{principal_name!r} already has an SSO identity from this issuer; unbind it first")
    row = TeamIdentity(team_id=team_id, principal_id=principal.id, issuer=issuer, subject=subject,
                       email=(email or "").strip() or None, bound_by=bound_by)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return identity_dict(row, principal.name)


async def unbind_identity(session: AsyncSession, *, principal_name: str, issuer: str | None = None,
                          team_id: int = TEAM_ID) -> int:
    """Operator action: remove a member's SSO binding; returns how many rows were removed."""
    issuer = (issuer or settings.BRAIN_OIDC_ISSUER or "").strip().rstrip("/")
    principal = await fresh(session, Principal, Principal.name == principal_name)
    if principal is None:
        raise TeamNotFound(f"Principal {principal_name!r} not found")
    predicates = [TeamIdentity.principal_id == principal.id, TeamIdentity.team_id == team_id]
    if issuer:
        predicates.append(TeamIdentity.issuer == issuer)
    result = await session.execute(delete(TeamIdentity).where(*predicates))
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
