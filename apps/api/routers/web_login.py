"""Web dashboard sign-in: one login and password, changed from the dashboard.

A fresh install has none, so the dashboard opens without a password and asks
the owner to set one. The password is stored only as a scrypt hash in the
checkout's ``.env`` (like the other settings), never returned. ``version`` is
a random token rotated on every change: the dashboard mixes it into its
session cookies, so a change signs every other browser out.

Forgot the password? Remove the ``WEB_LOGIN_*`` lines from the server's
``.env`` and restart the API: sign-in is open again.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.auth import require_api_key, require_scope
from apps.api.team_access import team_administrator_access
from brain.config.settings import settings
from brain.onboarding.envfile import setup_env_path, update_env_file

router = APIRouter(prefix="/api/web-login", tags=["web-login"], dependencies=[Depends(require_api_key)])
# Setting or removing the dashboard password changes the whole Brain's sign-in: a team administrator
# once the team has members. Reading the mode and checking a password stay part of the sign-in flow.
TEAM_ADMIN = Depends(team_administrator_access())

KEYS = frozenset({"WEB_LOGIN_USER", "WEB_LOGIN_PASSWORD_HASH", "WEB_LOGIN_VERSION"})
USER_PATTERN = re.compile(r"[A-Za-z0-9._@+-]{1,64}")
MIN_PASSWORD = 8
MAX_PASSWORD = 256

# scrypt cost: ~50 ms per check on a small VPS.
_N, _R, _P = 2**14, 8, 1

# Failed checks per login name; past the limit the name is refused until the
# window slides, so a public dashboard can't be brute-forced through the API.
# Failures are also counted per client address, so rotating login names from
# one host is throttled too. No trusted-proxy setting exists, so
# X-Forwarded-For is ignored and the socket peer address is used.
FAIL_LIMIT = 5
IP_FAIL_LIMIT = 20
FAIL_WINDOW_S = 300
_failures: Dict[str, Deque[float]] = defaultdict(deque)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    # ":" and base64url only, so the .env line needs no quoting or escaping.
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _configured() -> bool:
    return bool((settings.WEB_LOGIN_USER or "").strip() and (settings.WEB_LOGIN_PASSWORD_HASH or "").strip())


def _state() -> Dict[str, Any]:
    on = _configured()
    return {
        "mode": "password" if on else "open",
        "user": settings.WEB_LOGIN_USER if on else None,
        "version": settings.WEB_LOGIN_VERSION or "0",
        # Also set in the process environment (docker compose): that copy wins on restart.
        "from_env": sorted(k for k in KEYS if k in os.environ),
    }


def _client_ip(request: Optional[Request]) -> str:
    client = request.client if request is not None else None
    return client.host if client and client.host else "unknown"


def _over(key: str, limit: int) -> bool:
    window = _failures[key]
    cutoff = time.monotonic() - FAIL_WINDOW_S
    while window and window[0] < cutoff:
        window.popleft()
    return len(window) >= limit


def _throttled(user: str, ip: str) -> bool:
    return _over(f"user:{user.lower()}", FAIL_LIMIT) or _over(f"ip:{ip}", IP_FAIL_LIMIT)


def _fail(user: str, ip: str) -> None:
    now = time.monotonic()
    _failures[f"user:{user.lower()}"].append(now)
    _failures[f"ip:{ip}"].append(now)


async def _matches(user: str, password: str) -> bool:
    """Constant work whether or not the name matches."""
    stored = settings.WEB_LOGIN_PASSWORD_HASH or ""
    password_ok = await asyncio.to_thread(check_password, password, stored)
    user_ok = hmac.compare_digest(user.encode("utf-8"), (settings.WEB_LOGIN_USER or "").encode("utf-8"))
    return password_ok and user_ok


def _write(values: Dict[str, str]) -> None:
    try:
        update_env_file(setup_env_path(), values, extra_keys=KEYS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write .env: {type(exc).__name__}") from exc
    for key, value in values.items():
        setattr(settings, key, value or None)


class VerifyBody(BaseModel):
    user: str = Field(max_length=200)
    password: str = Field(max_length=MAX_PASSWORD * 4)


class SetBody(BaseModel):
    user: str = Field(max_length=200)
    password: str = Field(max_length=MAX_PASSWORD * 4)
    # Required once a password exists.
    current_password: Optional[str] = Field(default=None, max_length=MAX_PASSWORD * 4)


class DisableBody(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD * 4)


async def _require_current(current: Optional[str], request: Request) -> None:
    if not _configured():
        return
    user = settings.WEB_LOGIN_USER or ""
    ip = _client_ip(request)
    if _throttled(user, ip):
        raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in a few minutes.")
    if not current or not await _matches(user, current):
        _fail(user, ip)
        raise HTTPException(status_code=403, detail="The current password is wrong.")


@router.get("", dependencies=[Depends(require_scope("setup:read"))])
async def get_web_login() -> Dict[str, Any]:
    return _state()


@router.post("/verify", dependencies=[Depends(require_scope("setup:read"))])
async def verify_web_login(body: VerifyBody, request: Request) -> Dict[str, Any]:
    if not _configured():
        return {"ok": False, "mode": "open"}
    ip = _client_ip(request)
    if _throttled(body.user, ip):
        raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in a few minutes.")
    ok = await _matches(body.user, body.password)
    if not ok:
        _fail(body.user, ip)
    return {"ok": ok, "mode": "password", "version": settings.WEB_LOGIN_VERSION or "0"}


@router.put("", dependencies=[Depends(require_scope("setup:write")), TEAM_ADMIN])
async def set_web_login(body: SetBody, request: Request) -> Dict[str, Any]:
    user = body.user.strip()
    if not USER_PATTERN.fullmatch(user):
        raise HTTPException(status_code=400, detail="Login: 1–64 letters, digits or . _ @ + -")
    if not MIN_PASSWORD <= len(body.password) <= MAX_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Password: at least {MIN_PASSWORD} characters.")
    if body.password.strip() != body.password or any(ch in body.password for ch in "\r\n\x00"):
        raise HTTPException(status_code=400, detail="Password: no spaces at the ends or line breaks.")
    await _require_current(body.current_password, request)
    hashed = await asyncio.to_thread(hash_password, body.password)
    _write({"WEB_LOGIN_USER": user, "WEB_LOGIN_PASSWORD_HASH": hashed, "WEB_LOGIN_VERSION": secrets.token_hex(8)})
    _failures.clear()
    return _state()


@router.post("/disable", dependencies=[Depends(require_scope("setup:write")), TEAM_ADMIN])
async def disable_web_login(body: DisableBody, request: Request) -> Dict[str, Any]:
    if not _configured():
        return _state()
    await _require_current(body.current_password, request)
    _write({"WEB_LOGIN_USER": "", "WEB_LOGIN_PASSWORD_HASH": "", "WEB_LOGIN_VERSION": secrets.token_hex(8)})
    return _state()
