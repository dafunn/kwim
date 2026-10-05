"""Team provisioning and key management for the admin console. Each route writes
one audit row (`audited`). Team-scoped routes resolve the team through TeamPath;
create and adopt do not. See docs/DESIGN.md, "Provisioning teams" and "Preview
and confirm in the console".
"""
import logging
import secrets
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import APIRouter, HTTPException, status

from .. import provision
from ..admin_auth import AdminContext, CurrentOperator
from ..admin_key import CAPABILITIES
from ..auth import invalidate_key_cache
from ..config import settings
from ..keys import generate_key
from ..models import (
    KeyMintRequest,
    TeamCreateRequest,
    TeamDecommissionRequest,
    TeamDestroyRequest,
)
from ..runtime import State
from ..stores.postgres import _IDENT
from .admin_read import TeamPath
from .admin_write import audited

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# Names a team can never take: Postgres system schemas, kwim_admin and universe.
_RESERVED_TEAM_NAMES = frozenset({
    "public", "information_schema", "pg_catalog", "pg_toast", "kwim_admin", "universe",
})

# Preview token TTL (mirrors admin_write.py's forget preview): 5 minutes,
# single-use via GETDEL.
_PREVIEW_TTL = 300


def _require_team_create_enabled() -> None:
    if not settings.admin_allow_team_create:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="team creation disabled - set KWIM_ADMIN_ALLOW_TEAM_CREATE=true")


def _require_team_destroy_enabled() -> None:
    if not settings.admin_allow_team_destroy:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="team destroy disabled - set KWIM_ADMIN_ALLOW_TEAM_DESTROY=true")


async def _require_decommissioned(team: str) -> None:
    row = await State.admin.get_team(team)
    if row is None or row["status"] != "decommissioned":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"team {team!r} must be decommissioned first - "
                   f"POST /v1/admin/teams/{team}/decommission")


# ---------------------------------------------------------------------------
# Create / adopt
# ---------------------------------------------------------------------------

@router.post("/teams", status_code=status.HTTP_201_CREATED)
async def admin_create_team(body: TeamCreateRequest, operator: AdminContext = CurrentOperator):
    team = body.team
    if team == "universe":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'universe' is the cluster-wide shared schema, provisioned once - "
                   "not a team")
    if not _IDENT.match(team) or team in _RESERVED_TEAM_NAMES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"invalid or reserved team identifier: {team!r}")

    async with audited(operator, "team.create", team,
                       detail={"display_name": body.display_name}):
        _require_team_create_enabled()

        preflight = await State.pg.create_team_preflight()
        if not preflight["can_create"]:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=f"role {preflight['role']!r} cannot create a schema")

        already_existed = team in await State.pg.list_team_schemas()
        try:
            # Read at call time so tests can replace it.
            template = provision.load_team_schema_template(provision.TEMPLATE_PATH)
            rendered = provision.render_team_schema(template, team)
            await State.pg.apply_team_schema(rendered, team=team)
        except psycopg.Error as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"schema {team!r} exists with an incompatible shape: {exc}")

        await State.falkor.init_team_graph(team)
        await State.admin.create_team_record(team, display_name=body.display_name,
                                             created_by=operator.operator_id)
        return {"team": team, "schema_created": True, "graph_initialized": True,
                "already_existed": already_existed}


@router.post("/teams/{team}/adopt")
async def admin_adopt_team(team: str, operator: AdminContext = CurrentOperator):
    if not _IDENT.match(team):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="invalid team identifier")

    async with audited(operator, "team.adopt", team):
        if team not in await State.pg.list_team_schemas():
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"no schema exists for {team!r} - nothing to adopt")
        await State.admin.create_team_record(team)
        return {"team": team, "adopted": True}


# ---------------------------------------------------------------------------
# Decommission / restore
# ---------------------------------------------------------------------------

@router.post("/teams/{team}/decommission")
async def admin_decommission_team(body: TeamDecommissionRequest, team: str = TeamPath,
                                  operator: AdminContext = CurrentOperator):
    async with audited(operator, "team.decommission", team, detail={"reason": body.reason}):
        now = datetime.now(UTC)
        await State.admin.set_team_status(team, "decommissioned", decommissioned_at=now)
        revoked = await State.admin.revoke_all_team_keys(team)
        for row in revoked:
            invalidate_key_cache(row["key_prefix"])
        return {"team": team, "keys_revoked": len(revoked)}


@router.post("/teams/{team}/restore")
async def admin_restore_team(team: str = TeamPath, operator: AdminContext = CurrentOperator):
    async with audited(operator, "team.restore", team):
        await State.admin.set_team_status(team, "active", decommissioned_at=None)
        return {"team": team}


# ---------------------------------------------------------------------------
# Destroy (irreversible)
# ---------------------------------------------------------------------------

@router.post("/teams/{team}/destroy/preview")
async def admin_destroy_preview(team: str = TeamPath, operator: AdminContext = CurrentOperator):
    async with audited(operator, "team.destroy_preview", team, detail={"phase": "preview"}):
        _require_team_destroy_enabled()
        await _require_decommissioned(team)

        facts = await State.falkor.count_facts_admin(team)
        rules = await State.falkor.count_rules_admin(team)
        semantic = await State.falkor.count_semantic(team)
        episodic = await State.pg.count_episodic(team)
        commit_rows = await State.pg.count_commit_log(team)
        pending = (await State.pg.pending_stats(team))["count"]
        preflight = await State.pg.destroy_team_preflight(team)

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=_PREVIEW_TTL)
        # The token holds only the team; execute re-reads the row count.
        await State.falkor.destroy_preview_set(token, {"team": team}, _PREVIEW_TTL)

        return {
            "preview_token": token,
            "expires_at": expires_at.isoformat(),
            "counts": {"facts": facts, "rules": rules, "semantic": semantic,
                      "episodic": episodic, "commit_rows": commit_rows, "pending": pending},
            "objects": {"schema": team, "graphs": [f"kwim_{team}", f"kwim_{team}_code"]},
            "preflight": preflight,
        }


@router.post("/teams/{team}/destroy")
async def admin_destroy_team(body: TeamDestroyRequest, team: str = TeamPath,
                             operator: AdminContext = CurrentOperator):
    async with audited(operator, "team.destroy", team,
                       detail={"phase": "execute", "confirm_team": body.confirm_team,
                              "confirm_commit_rows": body.confirm_commit_rows}):
        _require_team_destroy_enabled()
        await _require_decommissioned(team)

        # Atomic get-and-delete: single-use enforced by the store.
        preview = await State.falkor.destroy_preview_getdel(body.preview_token)
        if preview is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail="preview token expired, already used, or unknown - "
                                       "re-run destroy/preview")
        if preview["team"] != team or body.confirm_team != team:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="confirm_team does not match")

        live_rows = await State.pg.count_commit_log(team)
        if body.confirm_commit_rows != live_rows:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"confirm_commit_rows mismatch - {live_rows} row(s) in the log "
                       f"now, submitted {body.confirm_commit_rows}; the team changed "
                       f"since the preview, so re-run destroy/preview")

        preflight = await State.pg.destroy_team_preflight(team)
        if not preflight["can_drop"]:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=f"role {preflight['role']!r} cannot drop schema {team!r}")

        # Graphs first: they can be rebuilt from the schema.
        dropped_graphs = await State.falkor.drop_team_graphs(team)
        await State.pg.drop_team_schema(team)
        await State.admin.delete_team_record(team)

        return {"team": team, "dropped": {"schema": team, "graphs": sorted(dropped_graphs)}}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/keys")
async def admin_list_team_keys(team: str = TeamPath, include_revoked: bool = False,
                               _operator: AdminContext = CurrentOperator):
    return {"items": await State.admin.list_api_keys(team=team, include_revoked=include_revoked)}


@router.post("/teams/{team}/keys", status_code=status.HTTP_201_CREATED)
async def admin_mint_key(body: KeyMintRequest, team: str = TeamPath,
                         operator: AdminContext = CurrentOperator):
    unknown = sorted(set(body.capabilities) - set(CAPABILITIES))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"unknown capability value(s): {unknown}")

    capabilities = sorted(set(body.capabilities))
    async with audited(operator, "key.create", team, object_type="api_key",
                       detail={"label": body.label, "capabilities": capabilities}):
        console_row = await State.admin.get_team(team)
        if console_row is not None and console_row["status"] == "decommissioned":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"team {team!r} is decommissioned - restore it before minting a key")

        full_key, key_prefix, key_hash = generate_key()
        row = await State.admin.create_api_key(
            team=team, label=body.label, key_prefix=key_prefix, key_hash=key_hash,
            capabilities=capabilities, created_by=operator.operator_id,
            expires_at=body.expires_at)
        return {"id": str(row["id"]), "key_prefix": row["key_prefix"],
                "capabilities": row["capabilities"], "key": full_key}


@router.delete("/teams/{team}/keys/{key_id}")
async def admin_revoke_key(key_id: str, team: str = TeamPath,
                           operator: AdminContext = CurrentOperator):
    async with audited(operator, "key.revoke", team, object_type="api_key", object_id=key_id):
        # A key belonging to another team is a 404.
        live = await State.admin.list_api_keys(team=team, include_revoked=False)
        if not any(k["key_prefix"] == key_id for k in live):
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"no live key with prefix {key_id!r} for team {team!r}")

        row = await State.admin.revoke_api_key(key_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                detail=f"no live key with prefix {key_id!r}")
        invalidate_key_cache(key_id)
        return {"id": str(row["id"]), "revoked_at": row["revoked_at"]}
