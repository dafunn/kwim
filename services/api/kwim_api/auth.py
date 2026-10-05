"""Team authentication: `current_team` resolves the bearer key to a TeamContext.
The team always comes from the key, never from the request.

Store-format keys (`kwim_<prefix>_<secret>`) are looked up in kwim_admin.api_keys
and cached; legacy KWIM_API_KEYS entries are the fallback. See docs/DESIGN.md,
"Team keys".
"""
import hmac
import logging
import os
import time
from dataclasses import dataclass

import psycopg
from fastapi import Depends, Header, HTTPException, status

from .config import settings
from .keys import hash_secret, parse_key
from .runtime import State

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamContext:
    team: str                              # tenant id -> Postgres schema, FalkorDB graph scope, RMQ routing segment
    key_id: str                            # which key authenticated (for audit), never the secret itself
    capabilities: frozenset[str] = frozenset()


# --- Legacy env-map fallback (see module docstring) ---

def _load_key_map() -> dict[str, str]:
    """key -> team. Placeholder store; replaced by the kwim_admin-backed path above."""
    raw = os.environ.get("KWIM_API_KEYS", "")
    out: dict[str, str] = {}
    for pair in (p for p in raw.split(",") if p.strip()):
        key, _, team = pair.partition(":")
        if key and team:
            out[key.strip()] = team.strip()
    return out


_KEY_MAP = _load_key_map()


def _legacy_capabilities(key_id: str) -> frozenset[str]:
    """Capabilities for a legacy key, from the promote_keys and review_keys lists."""
    promote_ids = {p.strip() for p in settings.promote_keys.split(",") if p.strip()}
    review_ids = {p.strip() for p in settings.review_keys.split(",") if p.strip()}
    caps = set()
    if key_id in promote_ids:
        caps.add("promote")
    if key_id in review_ids:
        caps.add("review")
    return frozenset(caps)


# Legacy key_ids already logged as not imported; each is logged once per process.
_legacy_reported: set[str] = set()


def _resolve_legacy_key(key: str) -> TeamContext:
    team = _KEY_MAP.get(key)
    if not team:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown team key.")
    # key_id = a non-secret handle (first 6 chars) purely for audit logging.
    key_id = key[:6]
    if key_id not in _legacy_reported:
        _legacy_reported.add(key_id)
        log.info("team %r is still authenticating via the legacy KWIM_API_KEYS "
                 "fallback (key_id=%s) - migrate it with kwim_api.admin_import_keys",
                 team, key_id)
    return TeamContext(team=team, key_id=key_id, capabilities=_legacy_capabilities(key_id))


# --- Store-backed resolution (the live path) ---

class _SchemaMissing(Exception):
    """kwim_admin does not exist yet - the caller falls back to the env map."""


_schema_missing_warned = False


def _warn_schema_missing_once() -> None:
    global _schema_missing_warned
    if not _schema_missing_warned:
        log.warning(
            "kwim_admin schema not found - team-key resolution is falling back to "
            "KWIM_API_KEYS until the admin schema is provisioned (see deployment.md)."
        )
        _schema_missing_warned = True


# prefix -> (key_hash, TeamContext, monotonic expiry). A hit skips the database,
# never the secret comparison.
_CACHE: dict[str, tuple[str, TeamContext, float]] = {}


def invalidate_key_cache(key_prefix: str | None = None) -> None:
    """Drop one cached key resolution, or all of them when prefix is None."""
    if key_prefix is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key_prefix, None)


def _cache_get(prefix: str) -> tuple[str, TeamContext] | None:
    hit = _CACHE.get(prefix)
    if hit is None:
        return None
    key_hash, ctx, expires_at = hit
    if time.monotonic() >= expires_at:
        _CACHE.pop(prefix, None)
        return None
    return key_hash, ctx


def _cache_put(prefix: str, key_hash: str, ctx: TeamContext) -> None:
    _CACHE[prefix] = (key_hash, ctx, time.monotonic() + settings.admin_key_cache_ttl_seconds)


async def _resolve_store_key(prefix: str, secret: str) -> TeamContext:
    """Resolve a store-format key. Raises HTTPException(401) for an unknown,
    revoked, expired or wrong-secret key; only `_SchemaMissing` falls back."""
    cached = _cache_get(prefix)
    if cached is not None:
        key_hash, ctx = cached
        if not hmac.compare_digest(hash_secret(secret), key_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown team key.")
        return ctx

    try:
        row = await State.admin.get_api_key(prefix)
    except psycopg.errors.UndefinedTable:
        _warn_schema_missing_once()
        raise _SchemaMissing from None

    if row is None or not hmac.compare_digest(hash_secret(secret), row["key_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown team key.")

    ctx = TeamContext(team=row["team"], key_id=row["key_prefix"],
                      capabilities=frozenset(row["capabilities"] or ()))
    _cache_put(prefix, row["key_hash"], ctx)
    # Once per cache fill, best-effort: a failed write does not fail the request.
    try:
        await State.admin.touch_api_key_last_used(prefix)
    except Exception:
        log.warning("last_used_at update failed for key_id=%s - authentication "
                    "unaffected", prefix)
    return ctx


async def current_team(authorization: str = Header(default="")) -> TeamContext:
    """FastAPI dependency: validate `Authorization: Bearer <key>` -> TeamContext."""
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization: Bearer <team-key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parsed = parse_key(key)
    if parsed is None:
        return _resolve_legacy_key(key)

    prefix, secret = parsed
    try:
        return await _resolve_store_key(prefix, secret)
    except _SchemaMissing:
        return _resolve_legacy_key(key)


CurrentTeam = Depends(current_team)


def require_capability(team: TeamContext, capability: str) -> None:
    """Raise 403 unless the authenticated key carries `capability`."""
    if capability not in team.capabilities:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail=f"key lacks the {capability!r} capability")
