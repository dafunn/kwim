"""Operator identity for /v1/admin: `current_operator` resolves a session token.
Separate from team keys; see docs/DESIGN.md, "Operators".
"""
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import Depends, Header, HTTPException, Request, status

from .config import settings
from .runtime import State

log = logging.getLogger(__name__)

_COOKIE_NAME_SECURE = "__Host-kwim_admin_session"
_COOKIE_NAME_INSECURE = "kwim_admin_session"


def cookie_name() -> str:
    """The session cookie's name for the admin.secure_cookie setting."""
    return _COOKIE_NAME_SECURE if settings.admin_secure_cookie else _COOKIE_NAME_INSECURE


def warn_if_insecure_cookie() -> None:
    """Log a warning at startup when the session cookie is not Secure."""
    if not settings.admin_secure_cookie:
        log.warning(
            "KWIM_ADMIN_SECURE_COOKIE=false - the admin session cookie is sent "
            "without Secure/__Host-. Only use this on a LAN-only deployment."
        )


@dataclass(frozen=True)
class AdminContext:
    operator_id: str
    username: str


def _require_admin_enabled() -> None:
    if not settings.admin_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="admin console disabled - set KWIM_ADMIN_ENABLED=true to enable")


def _extract_session_token(request: Request, authorization: str) -> str | None:
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return request.cookies.get(cookie_name())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


async def _refresh_session(token_hash: str, row: dict) -> None:
    """Extend the session by admin.session_ttl_hours, capped at
    admin.session_max_age_days from creation. Best-effort."""
    now = datetime.now(UTC)
    max_expiry = row["created_at"] + timedelta(days=settings.admin_session_max_age_days)
    new_expiry = min(now + timedelta(hours=settings.admin_session_ttl_hours), max_expiry)
    try:
        await State.admin.refresh_session(token_hash, new_expiry)
    except Exception:
        log.warning("admin session refresh failed - continuing with the existing expiry",
                    exc_info=True)


async def current_operator(
    request: Request, authorization: str = Header(default=""),
) -> AdminContext:
    """FastAPI dependency for /v1/admin. Resolves an opaque session token."""
    _require_admin_enabled()

    token = _extract_session_token(request, authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing admin session.")

    token_hash = hash_token(token)
    try:
        row = await State.admin.get_session(token_hash)
    except psycopg.errors.UndefinedTable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="admin schema not provisioned - apply db/admin-schema.sql")

    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired admin session.")

    await _refresh_session(token_hash, row)
    return AdminContext(operator_id=str(row["operator_id"]), username=row["username"])


CurrentOperator = Depends(current_operator)
