"""Operator login and logout for /v1/admin.

Issues and revokes the session that every other /v1/admin route requires via
`current_operator`.
"""
import datetime
import logging
import time

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from ..admin_auth import (
    _extract_session_token,
    _require_admin_enabled,
    cookie_name,
    generate_session_token,
    hash_token,
)
from ..config import settings
from ..keys import hash_password, verify_password
from ..models import AdminLoginRequest, AdminSessionResponse
from ..runtime import State

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# Verified against for unknown usernames, so every login runs scrypt.
_DUMMY_PASSWORD_HASH = hash_password("kwim-admin-timing-safety-dummy")

# {"user:<username>" | "addr:<source>": [failure monotonic timestamps]}; a key is
# dropped when its window empties. See docs/DESIGN.md, "Login protection".
_failures: dict[str, list[float]] = {}


def _client_address(request: Request) -> str:
    """The per-source counter's key: the first X-Forwarded-For hop, else the peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune_and_count(key: str) -> int:
    cutoff = time.monotonic() - settings.admin_login_lockout_minutes * 60
    live = [t for t in _failures.get(key, ()) if t > cutoff]
    if live:
        _failures[key] = live
    else:
        _failures.pop(key, None)
    return len(live)


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.monotonic())


def _locked_out(username: str, source: str) -> bool:
    max_failures = settings.admin_login_max_failures
    return (_prune_and_count(f"user:{username}") >= max_failures
            or _prune_and_count(f"addr:{source}") >= max_failures)


@router.post("/session", response_model=AdminSessionResponse)
async def admin_login(body: AdminLoginRequest, request: Request, response: Response):
    _require_admin_enabled()
    source = _client_address(request)

    if _locked_out(body.username, source):
        await State.admin.record(
            operator_id=None, action="admin.login", result="denied",
            object_type="session", detail={"reason": "locked_out"},
        )
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="too many failed login attempts - try again later")

    try:
        row = await State.admin.get_operator_by_username(body.username)
    except psycopg.errors.UndefinedTable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="admin schema not provisioned - apply db/admin-schema.sql")

    # Same response and timing whether or not the username exists.
    if row is not None and row["is_active"]:
        ok = verify_password(body.password, row["password_hash"])
    else:
        verify_password(body.password, _DUMMY_PASSWORD_HASH)
        ok = False

    if not ok:
        _record_failure(f"user:{body.username}")
        _record_failure(f"addr:{source}")
        # The submitted username is recorded only if it names a real operator.
        await State.admin.record(
            operator_id=str(row["id"]) if row else None, action="admin.login",
            result="denied", object_type="session",
            detail={"username": body.username} if row else {"username": "<unknown>"},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")

    _failures.pop(f"user:{body.username}", None)

    token = generate_session_token()
    now = datetime.datetime.now(datetime.UTC)
    expires_at = now + datetime.timedelta(hours=settings.admin_session_ttl_hours)
    session = await State.admin.create_session(
        operator_id=row["id"], token_hash=hash_token(token), expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
    )
    await State.admin.touch_operator_login(row["id"])
    await State.admin.record(
        operator_id=str(row["id"]), action="admin.login", result="ok",
        object_type="session", object_id=str(session["id"]), detail={"username": body.username},
    )

    # Max-Age covers the longest possible session; the server's expires_at decides.
    response.set_cookie(
        cookie_name(), token, httponly=True, secure=settings.admin_secure_cookie,
        samesite="strict", path="/",
        max_age=int(settings.admin_session_max_age_days * 24 * 3600),
    )
    return AdminSessionResponse(token=token, expires_at=expires_at)


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(request: Request, response: Response, authorization: str = Header(default="")):
    _require_admin_enabled()
    response.delete_cookie(cookie_name(), path="/")

    token = _extract_session_token(request, authorization)
    if not token:
        return

    try:
        revoked = await State.admin.revoke_session(hash_token(token))
    except psycopg.errors.UndefinedTable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="admin schema not provisioned - apply db/admin-schema.sql")

    if revoked is not None:
        await State.admin.record(
            operator_id=str(revoked["operator_id"]), action="admin.logout",
            result="ok", object_type="session", object_id=str(revoked["id"]),
        )
