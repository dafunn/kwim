"""The governance gate: consumes proposals from the bus and commits, rejects, or
routes each one to human review. A commit appends <team>.commit_log, then writes
the node and its edges to the team's graph.

See docs/DESIGN.md, "The governance gate" and "The commit log is the source of truth".
"""
import json
import logging
import uuid
from datetime import UTC, datetime
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
    Only the well-formed ones may reach SQL; see docs/DESIGN.md, "The governance gate"."""
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

        # A stable object_id whose node already exists (even retracted) is a no-op.
        # No proposer sets it today; see docs/DESIGN.md, "The governance gate".
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
        Fails open: the fact still commits without a vector."""
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

        # A stable-id fact is deduplicated by its id, not by similarity.
        if body.get("object_id"):
            return None, vec

        neighbors = await self._falkor.query_similar_facts(
            team, vec, k=5,
            about=body.get("about") or None,
            fact_type=body.get("fact_type"),
        )
        # The fact being superseded (and a stable-id fact's own node) is not a duplicate.
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

        # Malformed ids count as unknown evidence and never reach SQL.
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
        """Commit a proposal: append commit_log, then write the node.

        Used by auto-commit and by human approval. `extra_provenance` is merged in
        without overriding the proposer's attribution. A fact without `embedding`
        is embedded here.
        """
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

        # Every auto-commit is posted for review after the fact.
        if gate_decision == "auto_committed":
            await self._notify_auto_commit(team, object_id, ptype, body)

        return doc

    async def amend_object(
        self, team: str, object_id: str, object_type: str,
        new_payload: dict[str, Any], amended_by: str, amended_via: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Overwrite a live object's content fields, logging the previous values
        and the reason. See docs/DESIGN.md, "Changing committed objects".

        Returns {"status": "amended"|"not_found"|"not_current"|"invalid_field", ...}.
        """
        allowed = FalkorStore.AMENDABLE.get(object_type)
        if allowed is None:
            return {"status": "invalid_field", "fields": [], "detail":
                    f"amend does not apply to object_type {object_type!r}"}
        bad = sorted(set(new_payload) - set(allowed))
        if bad:
            return {"status": "invalid_field", "fields": bad, "detail":
                    f"not amendable: {', '.join(bad)}; amendable: {', '.join(allowed)}"}
        if not new_payload:
            return {"status": "invalid_field", "fields": [], "detail": "empty payload"}

        reader = {"fact": self._falkor.get_fact_content,
                  "rule": self._falkor.get_rule_content,
                  "semantic": self._falkor.get_semantic_content}[object_type]
        current = await reader(team, object_id)
        if current is None:
            return {"status": "not_found", "object_id": object_id}
        live = {"fact": "current", "rule": "approved"}.get(object_type)
        if live is not None and current.get("status") != live:
            return {"status": "not_current", "object_id": object_id,
                    "object_status": current.get("status")}

        # Only the fields this amend replaces.
        previous = {k: current.get(k) for k in new_payload}
        seq = await self._pg.append_commit(team, {
            "object_type": object_type, "object_id": object_id, "operation": "amend",
            "payload": new_payload,
            "provenance": {"amended_by": amended_by, "amended_via": amended_via,
                           "previous_payload": previous,
                           **({"reason": reason} if reason else {})},
            "proposed_by": None, "source_kind": None,
            "gate_decision": "human_approved",
        })

        embedding = None
        if object_type == "fact" and "statement" in new_payload:
            embedding = await self._embed_statement(new_payload["statement"])
            ok = await self._falkor.amend_fact(team, object_id, new_payload, seq,
                                               embedding=embedding)
        elif object_type == "fact":
            ok = await self._falkor.amend_fact(team, object_id, new_payload, seq)
        elif object_type == "rule":
            ok = await self._falkor.amend_rule(team, object_id, new_payload, seq,
                                               previous_situation=current.get("situation"))
        else:
            if "content" in new_payload:
                vecs = await self._embedder.embed([new_payload["content"]])
                embedding = vecs[0]
            ok = await self._falkor.amend_semantic(
                team, object_id, new_payload,
                previous_metadata=current.get("metadata"), embedding=embedding)

        if not ok:
            log.warning("gate: amend %s wrote commit_log seq=%s but the node did not "
                        "update - a rebuild will apply it", object_id, seq)
        log.info("gate: AMEND %s (%s) by %s via %s seq=%s",
                 object_id, object_type, amended_by, amended_via, seq)
        return {"status": "amended", "object_id": object_id, "seq": seq,
                "previous_payload": previous}

    async def commit_semantic(
        self, team: str, item_id: str, content: str, metadata: dict[str, Any],
        proposed_by: str | None = None,
    ) -> dict[str, Any]:
        """Commit a directly written semantic item: commit_log row, then the node.
        Unscreened, recorded as "auto_committed". See docs/DESIGN.md, "Semantic memory".
        Returns {"object_id", "seq"}.
        """
        payload = {"content": content, "metadata": metadata or {}}
        provenance = {"proposed_by": proposed_by} if proposed_by else {}
        seq = await self._pg.append_commit(team, {
            "object_type": "semantic", "object_id": item_id, "operation": "commit",
            "payload": payload, "provenance": provenance,
            "proposed_by": proposed_by,
            "source_kind": "agent_proposal",
            "gate_decision": "auto_committed",
        })
        vecs = await self._embedder.embed([content])
        await self._falkor.materialize_semantic(team, {
            "id": item_id, "content": content, "embedding": vecs[0],
            "metadata": metadata or {},
        })
        return {"object_id": item_id, "seq": seq}

    async def _route_to_review(
        self, team: str, pid: str, ptype: str, body: dict[str, Any], proposal: dict[str, Any],
        detail: str = "queued for human review",
    ) -> dict[str, Any]:
        """Write the proposal to pending_proposals, then set its status and notify.
        If the write fails, the message is dropped and the proposal logged in full."""
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
        """Post a newly pending proposal to Mattermost. Never raises."""
        if not settings.mm_webhook_url:
            return

        summary = summarize_proposal(ptype, body)
        title = f"KWIM review: {ptype} proposal ({team})"
        # A code span shows regex and markdown characters literally; backticks
        # inside are replaced so they cannot end the span.
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
        """Post an auto-committed object to Mattermost with Confirm / Retract
        buttons for /v1/review/committed-action. Never raises."""
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
        """Retract a committed object: a `retract` log row, then status 'retracted'.
        Returns {"status": "not_found" | "already_retracted" | "retracted", ...}.
        """
        found = await self._falkor.find_object(team, object_id, object_type)
        if found is None:
            return {"status": "not_found"}
        resolved_type, current_status = found
        if current_status == "retracted":
            return {"status": "already_retracted"}

        now = datetime.now(UTC).isoformat()
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
        """Confirm a committed object: a `confirm` log row, then confirmed_by /
        confirmed_at on the node. Returns {"status": "not_found" | "confirmed", ...}.
        """
        found = await self._falkor.find_object(team, object_id, object_type)
        if found is None:
            return {"status": "not_found"}
        resolved_type, _current_status = found

        now = datetime.now(UTC).isoformat()
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
        """Forget a committed object (see docs/DESIGN.md, "Forget"). `by` and `via`
        go to the service log only, since the object's log rows are deleted with it.
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
        """Forget the source events of an uncommitted proposal, keeping any that
        support a live object. Returns {"status": "no_delete" | "preflight_failed" |
        "forgotten", ...}.
        """
        result = await forget.forget_episodics(self._falkor, self._pg, team, episodic_ids)
        if result["status"] == "forgotten":
            log.warning("gate: FORGET %d episodic(s) by %s via %s (team %s)",
                        result.get("episodic_events"), by, via, team)
        return result

    async def _reinforce(
        self, team: str, pid: str, proposal: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        """Add evidence to an approved advisory rule and commit directly. Unknown
        evidence ids are dropped and logged; the count is the valid ids only."""
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
