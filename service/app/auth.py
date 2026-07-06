"""Per-team authentication: a bearer key resolves to a team identity, server-side.

This is the tenancy boundary. The team is never taken from a request parameter -
only from the validated key - so one team cannot act as another. Every routed
request depends on `current_team`, which yields the caller's `TeamContext`.

The skeleton resolves keys from an env map (`KWIM_API_KEYS="key1:teamA,key2:teamB"`)
so the service runs without the real key store. TODO: back this with your secret
manager (the same place per-team keys are minted), with rotation.
"""
import os
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status


@dataclass(frozen=True)
class TeamContext:
    team: str          # tenant id -> Postgres schema, FalkorDB graph scope, RMQ routing segment
    key_id: str        # which key authenticated (for audit), never the secret itself


def _load_key_map() -> dict[str, str]:
    """key -> team. Placeholder store; replace with secret-manager-backed resolution."""
    raw = os.environ.get("KWIM_API_KEYS", "")
    out: dict[str, str] = {}
    for pair in (p for p in raw.split(",") if p.strip()):
        key, _, team = pair.partition(":")
        if key and team:
            out[key.strip()] = team.strip()
    return out


_KEY_MAP = _load_key_map()


async def current_team(authorization: str = Header(default="")) -> TeamContext:
    """FastAPI dependency: validate `Authorization: Bearer <key>` -> TeamContext."""
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization: Bearer <team-key>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    team = _KEY_MAP.get(key)
    if not team:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown team key.")
    # key_id = a non-secret handle (first 6 chars) purely for audit logging.
    return TeamContext(team=team, key_id=key[:6])


CurrentTeam = Depends(current_team)
