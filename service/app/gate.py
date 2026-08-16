"""The governance gate

Consumes proposals off the bus (kwim.<team>.{knowledge,wisdom}.proposed), decides
commit vs. human-review by evidence/conflict, and on commit makes the write durable
and visible:
  1. append <team>.commit_log  (Postgres - the durable, replayable record)
  2. materialize the node + provenance edges in the team's FalkorDB graph
(1) before (2) is deliberate: the commit log is the source of truth; the graph is a
projection rebuilt from it, so the log must win if (2) ever fails.

Decision policy (v1):
  - fact:        evidence integrity + embedding screen -> dup reject / near review /
                 commit. Auto-commit after clear screen.
  - advisory:    evidence integrity + NELL-style distinct-session count;
                 auto-commit at session_count >= threshold, else review.
  - constraint:  always human review - enforcement policy is too consequential to
                 auto-commit. Never auto-applied.

Human review: proposals routed to "review" are persisted to
<team>.pending_proposals (durable - see db/team-schema.sql.j2) before the
proposal status KV is updated, so an approval/rejection always has a body to act
on. A best-effort Mattermost notification follows.

Gate verify: embedder-down -> screen skipped, commit with
provenance.verify="skipped:embedder_unavailable" (fail-open). Kill switch:
KWIM_GATE_VERIFY=0 bypasses all verify checks.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aio_pika
import httpx

from . import forget
from .config import settings
from .embedder import Embedder
from .freshness import resolve_decay_class
from .stores.bus import _EXCHANGE
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore

log = logging.getLogger(__name__)

_SUMMARY_MAX = settings.gate_summary_max


def _split_well_formed_uuids(ids: list[str]) -> tuple[list[str], list[str]]:
    """Partition evidence ids into (well-formed canonical UUID strings, malformed).

    Only well-formed ids may reach evidence_meta's `::uuid[]` cast - a malformed id
    (e.g. a model that hallucinated or truncated an evidence id) would otherwise throw
    InvalidTextRepresentation and crash the gate consumer, stalling all governance.
    Malformed ids are returned separately so callers can treat them as unknown."""
    well_formed: list[str] = []
    malformed: list[str] = []
    for eid in ids:
        try:
            well_formed.append(str(uuid.UUID(str(eid))))
        except (ValueError, TypeError, AttributeError):
            malformed.append(eid)
    return well_formed, malformed


def summarize_proposal(object_type: str, body: dict[str, Any]) -> str:
    """Human-readable one-liner for a pending proposal (review queue + Mattermost).

    fact -> statement, advisory -> approach, constraint -> action_pattern + verdict.
    Truncated to _SUMMARY_MAX chars.
    """
    if object_type == "fact":
        text = str(body.get("statement", ""))
    elif body.get("rule_type") == "constraint":
        text = f"{body.get('action_pattern', '')} -> {body.get('verdict', '')}"
    else:
        text = str(body.get("approach", ""))
    if len(text) > _SUMMARY_MAX:
        text = text[: _SUMMARY_MAX - 3] + "..."
    return text


class Gate:
    def __init__(
        self, pg: PostgresStore, falkor: FalkorStore,
        bus_channel: aio_pika.abc.AbstractChannel, embedder: Embedder | None = None,
    ):
        self._pg = pg
        self._falkor = falkor
        self._ch = bus_channel
        self._embedder = embedder

    async def run(self) -> None:
        """Bind a durable queue to all proposal routing keys and consume."""
        ex = await self._ch.declare_exchange(_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        q = await self._ch.declare_queue("kwim.gate", durable=True)
        await q.bind(ex, routing_key="kwim.*.knowledge.proposed")
        await q.bind(ex, routing_key="kwim.*.wisdom.proposed")
        await q.consume(self._on_message)

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            proposal = json.loads(message.body.decode())
            await self.handle(proposal)

    async def handle(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Evaluate one proposal. Returns the resolution doc (also persisted)."""
        team = proposal["team"]
        pid = proposal["proposal_id"]
        ptype = proposal["object_type"]          # 'fact' | 'rule'
        body = proposal["body"]

        # --- reinforce short-circuit (advisory only; before normal decide path) ---
        if ptype == "rule" and body.get("reinforces"):
            return await self._reinforce(team, pid, proposal, body)

        # --- idempotent re-distill short-circuit ---
        # A proposer-supplied stable object_id whose node already exists is a no-op:
        # that fact was committed and posted for review when first seen, so re-runs
        # don't re-commit, re-notify, or re-queue it. It also respects a human
        # retraction - a retracted node still "exists", so we won't resurrect it.
        #
        # Dormant: the code distiller supersedes-on-change. Kept for internal
        # raw-bus proposers.
        stable_id = body.get("object_id")
        if ptype == "fact" and stable_id and await self._falkor.find_object(team, stable_id, "fact"):
            doc = {"id": pid, "object_type": "fact", "status": "noop",
                   "detail": f"idempotent: object_id={stable_id} already committed"}
            await self._falkor.proposal_set(pid, doc)
            return doc

        # --- evidence integrity + NELL-style session counting ---
        embedding: list[float] | None = None
        extra_verify: dict[str, Any] = {}

        if settings.gate_verify_enabled:
            deduped_ids, session_count, ev_problem = await self._check_evidence(team, body)
            # Replace evidence with the deduped valid list so provenance edges are clean.
            body = {**body, "evidence": deduped_ids}

            if ev_problem:
                return await self._route_to_review(
                    team, pid, ptype, body, proposal,
                    detail=f"evidence integrity: {ev_problem}",
                )

            # --- fact-path: embedding screen ---
            if ptype == "fact":
                screen_doc, embedding = await self._screen_fact(team, pid, ptype, body, proposal)
                if screen_doc is not None:
                    return screen_doc                # reject or route-to-review returned
                if embedding is not None:
                    extra_verify = {"verify": "screened"}
                else:
                    extra_verify = {"verify": "skipped:embedder_unavailable"}
        else:
            session_count = len(body.get("evidence", []))

        gate_decision: str
        _, gate_decision = self._decide(ptype, body, session_count)
        if gate_decision == "human_approved":
            return await self._route_to_review(team, pid, ptype, body, proposal)

        return await self.commit_proposal(
            team, pid, ptype, body, proposal, gate_decision,
            extra_provenance=extra_verify if extra_verify else None,
            embedding=embedding,
        )

    async def _embed_statement(self, statement: str | None) -> list[float] | None:
        """Embed a fact statement for storage, or None if that is not possible.

        Fail-open, like the screen: a fact that cannot be embedded still commits.
        The cost of failing is a fact invisible to semantic search, which
        `app.backfill_embeddings` repairs, so it is logged loudly enough to notice.
        """
        if self._embedder is None or not statement or not statement.strip():
            return None
        try:
            return (await self._embedder.embed([statement]))[0]
        except Exception as exc:
            log.warning("gate: could not embed statement for commit, fact will be "
                        "invisible to semantic search until backfilled: %s", exc)
            return None

    async def _screen_fact(
        self, team: str, pid: str, ptype: str, body: dict[str, Any], proposal: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[float] | None]:
        """Embedding screen for facts.

        Returns (resolution_doc, embedding):
          - (None, vec)  - clear to commit; caller should materialize with vec.
          - (None, None) - embedder unavailable (fail-open); commit without vector.
          - (doc, None)  - rejected (dup) or routed to review (near-match); caller returns doc.
        """
        if self._embedder is None:
            return None, None
        try:
            vecs = await self._embedder.embed([body["statement"]])
            vec = vecs[0]
        except Exception as exc:
            log.warning("gate: embedder unavailable for fact screen, skipping: %s", exc)
            return None, None

        # A stable-id fact uses its object_id as the dedup key rather than embedding
        # similarity, so it skips the near-match screen and commits directly (still
        # storing the vector for retrieval). Without this, structurally-distinct hubs
        # with similar phrasing ("X is a call hub" / "Y is a call hub") read as
        # near-matches, perpetually route to review, never commit, and re-queue every
        # run. handle()'s idempotent short-circuit already drops re-proposals of ones
        # that already committed; this lets the new ones through cleanly.
        # Dormant along with that short-circuit.
        if body.get("object_id"):
            return None, vec

        neighbors = await self._falkor.query_similar_facts(
            team, vec, k=5,
            about=body.get("about") or None,
            fact_type=body.get("fact_type"),
        )
        # Exclude the explicit supersession target - the live path, used by the code
        # distiller, which proposes supersedes=<current id> when a statement changes -
        # and, for a stable-id proposer, the fact's own prior node, so re-proposing the
        # same structural fact updates it in place (MERGE-on-id in commit) instead of
        # dup-rejecting or review-spamming against itself.
        excluded = {body.get("supersedes"), body.get("object_id")}
        neighbors = [n for n in neighbors if n["id"] not in excluded]

        if not neighbors:
            return None, vec  # no similar facts in the index - clear to commit

        nearest = neighbors[0]
        score = nearest["score"]

        if score <= settings.gate_dup_distance:
            doc = {"id": pid, "object_type": ptype, "status": "rejected",
                   "detail": f"duplicate of fact {nearest['id']} (d={score:.3f})"}
            await self._falkor.proposal_set(pid, doc)
            log.info("gate: rejected fact %s as duplicate of %s (d=%.3f)", pid, nearest["id"], score)
            return doc, None

        if score <= settings.gate_review_distance:
            doc = await self._route_to_review(
                team, pid, ptype, body, proposal,
                detail=f"similar to fact {nearest['id']} (d={score:.3f}) - possible conflict/supersession",
            )
            return doc, None

        return None, vec  # clear to commit

    async def _check_evidence(
        self, team: str, body: dict[str, Any],
    ) -> tuple[list[str], int, str | None]:
        """Evidence integrity + NELL-style session count.

        Returns (deduped_valid_ids, distinct_session_count, problem_or_None).
        problem is set when any submitted id is unknown in episodic_events (-> review).
        Duplicate ids are silently deduplicated before the Postgres lookup.
        """
        raw_ids: list[str] = body.get("evidence", [])
        deduped = list(dict.fromkeys(raw_ids))          # stable dedup, preserves order

        if not deduped:
            return [], 0, None

        # Only well-formed UUIDs may reach evidence_meta's `::uuid[]` cast - a
        # malformed id (e.g. a model that hallucinated or truncated an evidence id,
        # like "9f007edb") would throw InvalidTextRepresentation and crash the gate
        # consumer, stalling all governance for every team. Canonicalize valid ids
        # (so case/format variants match the DB's id::text) and treat malformed ids
        # as unknown evidence -> problem -> review. Never send them to SQL.
        well_formed, malformed = _split_well_formed_uuids(deduped)
        rows = await self._pg.evidence_meta(team, well_formed) if well_formed else []
        found_ids = {r["id"] for r in rows}
        unknown = malformed + [eid for eid in well_formed if eid not in found_ids]
        valid_ids = [eid for eid in well_formed if eid in found_ids]

        session_count = len({r["session_id"] for r in rows})
        problem = f"unknown evidence ids: {unknown}" if unknown else None
        return valid_ids, session_count, problem

    async def commit_proposal(
        self, team: str, pid: str, ptype: str, body: dict[str, Any],
        proposal: dict[str, Any], gate_decision: str = "auto_committed",
        extra_provenance: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Commit a proposal: append commit_log + materialize the FalkorDB node.

        Shared by auto-commit (handle()) and human-approved commit (review
        surface) - identical in shape except gate_decision and reviewer
        provenance. extra_provenance (approved_by/approved_via, verify) is
        merged in without overriding the original proposer's attribution.
        `embedding`: stored on the :Fact node when present. Callers that already
        hold a vector (the auto-commit path, which screened with it) pass it in;
        anyone else leaves it None and this method embeds the statement itself, so
        no commit path can mint a fact that semantic search cannot see.
        """
        # A stable, body-supplied object_id lets an idempotent proposer re-commit the
        # same structural fact onto one node via materialize_fact's MERGE-on-id,
        # instead of minting a new uuid every run and accumulating duplicates. Agents
        # can't reach this: FactProposal has no object_id field, so only internal
        # raw-bus proposers can set it, and none does today (see handle()).
        object_id = body.get("object_id") or str(uuid.uuid4())
        payload, provenance = self._split(ptype, body, proposal)
        if ptype == "fact" and embedding is None:
            embedding = await self._embed_statement(payload.get("statement"))
        if extra_provenance:
            provenance = {**provenance, **extra_provenance}
        seq = await self._pg.append_commit(team, {
            "object_type": ptype, "object_id": object_id, "operation": "commit",
            "payload": payload, "provenance": provenance,
            "proposed_by": provenance.get("proposed_by"),
            "source_kind": body.get("source_kind", "agent_proposal"),
            "gate_decision": gate_decision,
        })
        if ptype == "fact":
            await self._falkor.materialize_fact(
                team, {**payload, "id": object_id, "commit_seq": seq}, provenance,
                embedding=embedding)
        elif ptype == "rule":
            await self._falkor.materialize_rule(
                team,
                {**payload, "id": object_id, "commit_seq": seq,
                 "status": "approved", "scope": "team"},
                provenance)

        doc = {"id": pid, "object_type": ptype, "status": "committed",
               "detail": f"object_id={object_id} seq={seq}",
               "object_id": object_id, "seq": seq}
        await self._falkor.proposal_set(pid, doc)

        # Every auto-commit is posted for post-hoc review (Confirm/Retract) - nothing
        # is committed silently, regardless of source_kind. (Human-approved commits
        # arrive with gate_decision='human_approved', so they aren't re-notified; and
        # unchanged re-distills never reach here - the idempotent short-circuit in
        # handle() drops them before commit.)
        if gate_decision == "auto_committed":
            await self._notify_auto_commit(team, object_id, ptype, body)

        return doc

    async def _route_to_review(
        self, team: str, pid: str, ptype: str, body: dict[str, Any], proposal: dict[str, Any],
        detail: str = "queued for human review",
    ) -> dict[str, Any]:
        """Persist the proposal durably before updating status/notifying.

        If insert_pending fails, the message is nacked (requeue=False) by
        _on_message's `message.process` context - the proposal is dropped, but
        logged in full so it's recoverable from Loki.
        """
        try:
            await self._pg.insert_pending(team, {
                "proposal_id": pid, "object_type": ptype,
                "proposed_by": proposal.get("proposed_by"),
                "body": body, "bus_message": proposal,
            })
        except Exception:
            log.error("gate: insert_pending failed for proposal %s, full proposal: %s",
                       pid, json.dumps(proposal), exc_info=True)
            raise

        doc = {"id": pid, "object_type": ptype, "status": "pending_review", "detail": detail}
        await self._falkor.proposal_set(pid, doc)
        await self._notify_review(team, pid, ptype, body)
        return doc

    async def _notify_review(self, team: str, pid: str, ptype: str, body: dict[str, Any]) -> None:
        """Best-effort mattermost notification for a newly-pending proposal.

        Never raises - webhook failure is logged and otherwise ignored. The
        review queue (GET /v1/review/pending) is the source of truth, not this
        notification.
        """
        if not settings.mm_webhook_url:
            return

        summary = summarize_proposal(ptype, body)
        title = f"KWIM review: {ptype} proposal ({team})"
        # Render the summary inside a markdown code span so regex/markdown special
        # chars in it (e.g. a constraint's action_pattern `.*foo.*`) display literally
        # in mattermost instead of being eaten as formatting. Escape backticks in the
        # summary first so they can't break out of the code span.
        text = f"`{summary.replace('`', '\u2032')}`" if summary else "(no summary available)"

        attachment: dict[str, Any] = {
            "fallback": f"{title}: {text}",
            "title": title,
            "text": text,
        }

        if settings.service_url and settings.mm_action_secret:
            action_url = f"{settings.service_url}/v1/review/mm-action"
            attachment["actions"] = [
                {
                    "id": "approve",
                    "name": "Approve",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "proposal_id": pid, "team": team, "decision": "approve",
                            "secret": settings.mm_action_secret,
                        },
                    },
                },
                {
                    "id": "reject",
                    "name": "Reject",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "proposal_id": pid, "team": team, "decision": "reject",
                            "secret": settings.mm_action_secret,
                        },
                    },
                },
                {
                    "id": "forget",
                    "name": "Forget",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "proposal_id": pid, "team": team, "decision": "forget",
                            "secret": settings.mm_action_secret,
                        },
                    },
                },
            ]
        else:
            attachment["text"] = f"{text}\n\nproposal_id: {pid}"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(settings.mm_webhook_url, json={"attachments": [attachment]})
                r.raise_for_status()
        except Exception as exc:
            log.warning("gate: Mattermost notify failed for proposal %s: %s", pid, exc)

    async def _notify_auto_commit(self, team: str, object_id: str, ptype: str, body: dict[str, Any]) -> None:
        """Best-effort mattermost notification for a distiller auto-commit.

        Distinct from `_notify_review`: this fires after commit (the object is
        already live), labeled "auto-committed (review optional)", and its buttons
        act on the committed `object_id` via `/v1/review/committed-action`
        (Confirm/Retract) rather than on a pending proposal_id. Never raises -
        webhook failure is logged and otherwise ignored.
        """
        if not settings.mm_webhook_url:
            return

        summary = summarize_proposal(ptype, body)
        title = f":robot_face: KWIM auto-committed (review optional): {ptype} ({team})"
        text = f"`{summary.replace('`', '\u2032')}`" if summary else "(no summary available)"

        attachment: dict[str, Any] = {
            "fallback": f"{title}: {text}",
            "title": title,
            "text": f"{text}\n\nobject_id: {object_id}",
        }

        if settings.service_url and settings.mm_action_secret:
            action_url = f"{settings.service_url}/v1/review/committed-action"
            attachment["actions"] = [
                {
                    "id": "confirm",
                    "name": "Confirm",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "object_id": object_id, "object_type": ptype, "team": team,
                            "decision": "confirm", "secret": settings.mm_action_secret,
                        },
                    },
                },
                {
                    "id": "retract",
                    "name": "Retract",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "object_id": object_id, "object_type": ptype, "team": team,
                            "decision": "retract", "secret": settings.mm_action_secret,
                        },
                    },
                },
                {
                    "id": "forget",
                    "name": "Forget",
                    "integration": {
                        "url": action_url,
                        "context": {
                            "object_id": object_id, "object_type": ptype, "team": team,
                            "decision": "forget", "secret": settings.mm_action_secret,
                        },
                    },
                },
            ]

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(settings.mm_webhook_url, json={"attachments": [attachment]})
                r.raise_for_status()
        except Exception as exc:
            log.warning("gate: Mattermost auto-commit notify failed for object %s: %s", object_id, exc)

    async def retract_object(
        self, team: str, object_id: str, by: str, via: str, object_type: str | None = None,
    ) -> dict[str, Any]:
        """Post-hoc retraction of an already-committed object.

        Appends a `commit_log` entry (operation="retract", gate_decision=
        "human_retracted") and flips the FalkorDB node's status to 'retracted' -
        mirrors the existing supersede path. `query_facts`/`query_rules` filter on
        the live status, so a retracted object stops being served immediately.
        Returns {"status": "not_found" | "already_retracted" | "retracted", ...}.
        """
        found = await self._falkor.find_object(team, object_id, object_type)
        if found is None:
            return {"status": "not_found"}
        resolved_type, current_status = found
        if current_status == "retracted":
            return {"status": "already_retracted"}

        now = datetime.now(timezone.utc).isoformat()
        seq = await self._pg.append_commit(team, {
            "object_type": resolved_type, "object_id": object_id, "operation": "retract",
            "payload": {}, "provenance": {"retracted_by": by, "retracted_via": via, "retracted_at": now},
            "proposed_by": None, "source_kind": None, "gate_decision": "human_retracted",
        })
        await self._falkor.retract_object(team, resolved_type, object_id)
        return {"status": "retracted", "object_id": object_id, "object_type": resolved_type, "seq": seq}

    async def confirm_object(
        self, team: str, object_id: str, by: str, via: str, object_type: str | None = None,
    ) -> dict[str, Any]:
        """Post-hoc human confirmation of an already-committed object.

        Appends a `commit_log` entry (operation="confirm", gate_decision=
        "human_confirmed") and stamps confirmed_by/confirmed_at on the FalkorDB
        node - no status change. Replay-able like retract, so rebuild preserves
        the confirmation.
        Returns {"status": "not_found" | "confirmed", ...}.
        """
        found = await self._falkor.find_object(team, object_id, object_type)
        if found is None:
            return {"status": "not_found"}
        resolved_type, _current_status = found

        now = datetime.now(timezone.utc).isoformat()
        seq = await self._pg.append_commit(team, {
            "object_type": resolved_type, "object_id": object_id, "operation": "confirm",
            "payload": {}, "provenance": {"confirmed_by": by, "confirmed_via": via, "confirmed_at": now},
            "proposed_by": None, "source_kind": None, "gate_decision": "human_confirmed",
        })
        await self._falkor.confirm_object(team, resolved_type, object_id, by, now)
        return {"status": "confirmed", "object_id": object_id, "object_type": resolved_type, "seq": seq}

    async def forget_object(
        self, team: str, object_id: str, by: str, via: str, object_type: str | None = None,
    ) -> dict[str, Any]:
        """Hard-forget an already-committed object - irreversibly remove it from every
        store (FalkorDB node + embedding, commit_log rows, non-shared source episodics).

        Unlike retract_object (soft: status flip, replayable), this leaves no tombstone,
        so a rebuild cannot re-derive it. The shared-evidence guard preserves any episodic
        that also supports a different live object. Delegates to the shared forget core,
        which preflights Postgres DELETE and aborts before touching FalkorDB if the role
        can't finish in Postgres (no half-forget). `by`/`via` are logged for the operator
        audit only - by design the governed audit rows are deleted with the object.
        Returns {"status": "not_found" | "preflight_failed" | "forgotten", ...}.
        """
        result = await forget.forget_one(
            self._falkor, self._pg, team, object_id, object_type=object_type)
        if result["status"] == "forgotten":
            log.warning("gate: FORGET object %s (%s) by %s via %s - removed %s",
                        object_id, result.get("type"), by, via,
                        {k: result.get(k) for k in ("objects", "commit_log_rows", "episodic_events")})
        return result

    async def forget_episodics(
        self, team: str, episodic_ids: list[str], by: str, via: str,
    ) -> dict[str, Any]:
        """Hard-forget the source episodics behind a rejected/uncommitted proposal.

        Nothing was committed (no graph node), so this only deletes the source episodic
        events - after the shared-evidence guard, so any event still supporting a live
        object is preserved. Delegates to the shared forget core (Postgres preflight
        included). `by`/`via` are logged for the operator audit only.
        Returns {"status": "no_delete" | "preflight_failed" | "forgotten", ...}.
        """
        result = await forget.forget_episodics(self._falkor, self._pg, team, episodic_ids)
        if result["status"] == "forgotten":
            log.warning("gate: FORGET %d episodic(s) by %s via %s (team %s)",
                        result.get("episodic_events"), by, via, team)
        return result

    async def _reinforce(
        self, team: str, pid: str, proposal: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        """Accrue evidence on an already-approved advisory rule (no new node created).

        Auto-commits: adding evidence to a live rule is not a new claim, so it
        skips the normal threshold/review path. Evidence is deduped and
        validated against episodic_events; n becomes the deduped-valid count
        (unknown ids are dropped, logged - not routed to review; the rule already
        exists, and a later reinforce will pick up in-flight events once they land).
        """
        rule_id = body["reinforces"]
        raw_ev: list[str] = body.get("evidence", [])

        # dedup + validate; use only real episodic ids for provenance and count.
        if settings.gate_verify_enabled and raw_ev:
            well_formed, malformed = _split_well_formed_uuids(list(dict.fromkeys(raw_ev)))
            rows = await self._pg.evidence_meta(team, well_formed) if well_formed else []
            found_ids = {r["id"] for r in rows}
            new_ev = [eid for eid in well_formed if eid in found_ids]
            dropped = malformed + [eid for eid in well_formed if eid not in found_ids]
            if dropped:
                log.warning("gate: _reinforce %s: dropping %d unknown/malformed evidence id(s): %s",
                             pid, len(dropped), dropped)
        else:
            new_ev = list(dict.fromkeys(raw_ev))
        n = len(new_ev)

        # Verify the target exists and is approved before touching the commit log.
        target = await self._falkor.get_rule(team, rule_id)
        if not target or target.get("status") != "approved":
            doc = {"id": pid, "object_type": "rule", "status": "rejected",
                   "detail": f"reinforces unknown/unapproved rule: {rule_id}"}
            await self._falkor.proposal_set(pid, doc)
            return doc

        provenance = {"proposed_by": proposal.get("proposed_by"), "learned_from": new_ev}
        seq = await self._pg.append_commit(team, {
            "object_type": "rule", "object_id": rule_id, "operation": "reinforce",
            "payload": {"evidence": new_ev},
            "provenance": provenance,
            "proposed_by": provenance["proposed_by"],
            "source_kind": body.get("source_kind", "agent_proposal"),
            "gate_decision": "auto_committed",
        })
        await self._falkor.reinforce_rule(team, rule_id, new_ev, seq)

        doc = {"id": pid, "object_type": "rule", "status": "committed",
               "detail": f"reinforced {rule_id} +{n} evidence seq={seq}"}
        await self._falkor.proposal_set(pid, doc)
        return doc

    def _decide(self, ptype: str, body: dict[str, Any], session_count: int = 0) -> tuple[str, str]:
        if ptype == "fact":
            return "commit", "auto_committed"
        if body.get("rule_type") == "constraint":
            return "review", "human_approved"          # never auto-commit enforcement policy
        # advisory: NELL-style distinct-session count instead of raw evidence length
        if session_count >= settings.gate_auto_commit_threshold:
            return "commit", "auto_committed"
        return "review", "human_approved"

    @staticmethod
    def _split(ptype: str, body: dict[str, Any], proposal: dict[str, Any]) -> tuple[dict, dict]:
        """Separate node content (payload) from edges (provenance)."""
        if ptype == "fact":
            fact_type = body["fact_type"]
            payload = {"statement": body["statement"], "fact_type": fact_type,
                       "source_kind": body.get("source_kind", "agent_proposal"),
                       "about": body.get("about", []),
                       "decay_class": resolve_decay_class(fact_type, body.get("decay_class"))}
            provenance = {"proposed_by": proposal.get("proposed_by"),
                          "supported_by": body.get("evidence", []),
                          "supersedes": body.get("supersedes")}
        else:  # rule
            payload = {k: v for k, v in body.items() if k != "evidence"}
            provenance = {"proposed_by": proposal.get("proposed_by"),
                          "learned_from": body.get("evidence", [])}
        return payload, provenance
