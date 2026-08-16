# kwim-service Contract - the framework-agnostic K/W/M API

This is the contract for the kwim-service - the component of the KWIM stack
that exposes Knowledge, Wisdom, and Memory. (Intelligence is a separate gateway -
LiteLLM, for routing + accounting.) It is the single interface every agent team
codes against, so it has to stay framework-agnostic; the contract is the
HTTP/JSON wire protocol, not a library. Thin per-language clients are
conveniences layered on top; the wire is the truth.

---

## 1. Foundational decisions

| # | Decision | Rationale |
|---|---|---|
| Transport | HTTP/JSON, versioned under `/v1/` | callable identically from any language/framework; thin clients optional. (gRPC rejected for v1 - heavier, worse for polyglot/casual callers.) |
| Identity & tenancy | per-team API key in `Authorization: Bearer ...`; team derived server-side | a team cannot spoof another via a `team=` param; key drives per-team Postgres schema + per-graph scoping. |
| Scope (v1) | K / W / M only | the surface the kwim-service owns within the KWIM stack. Intelligence (LiteLLM - the model gateway) and Tooling stay direct calls for now; unifying them under this contract is a later goal. |
| Sync/async | reads sync; gate-writes & episodic async | keeps propose-don't-write and event emission off the request critical path. Exception: `wisdom/check` is sync (it's enforcement - the agent waits for allow/deny). |

Cross-team reads: a team only ever sees its own data + the universe graph
(server-side merge). There is no contract surface for reading another team's data -
isolation is enforced below the API, not requested through it.

---

## 2. The surface

`->` = async (returns `202 {proposal_id}`); everything else is sync. All calls are
team-scoped by the bearer key.

### Knowledge
```
GET  /v1/knowledge/query            read facts by structured filters
       ?fact_type= &status=current &about= &limit=
       -> { facts: [ {id, statement, fact_type, status, created_at} ] }

GET  /v1/knowledge/search           semantic search over facts - free text, no tag needed
       ?q=<text> &limit= &fact_type= &about=
       -> [ {id, statement, ..., freshness, score} ]   # score = cosine distance, lower = closer
       # ranked nearest-first, not re-sorted by freshness; 503 if the embedder is down
       # (never a silent []). /query is the tag path; this is the "I don't know the
       # tag" path. Facts with no embedding cannot match - see app.backfill_embeddings.

GET  /v1/knowledge/facts/{id}       one fact + full provenance (edges)
       -> { fact: {...}, provenance: { supported_by:[...], proposed_by, supersedes } }

GET  /v1/knowledge/audit/{id}       "why believed X at T" - provenance walk
       ?at=<ts>
       -> { fact, chain: [ evidence -> agent -> supersession... ] }

POST /v1/knowledge/propose       ->  submit a fact proposal to the gate
       body: { statement, fact_type, evidence:[episodic_event_id...],
               supersedes?:fact_id, about?:[entity_ref...], source_kind:"agent_proposal" }
       -> 202 { proposal_id, status:"accepted" }
```

### Wisdom
```
GET  /v1/wisdom/rules               applicable rules for a situation (decision-time hot path)
       ?situation.<key>=<value> ...   (open team-defined situation keys, AND-matched)
       -> { rules: [ {id, rule_type, situation, approach, evidence_count, status} ] }   # ranked by evidence; rule_type distinguishes advisory/constraint

POST /v1/wisdom/propose          ->  submit a learned-rule candidate
       body(advisory):   { rule_type:"advisory", situation, approach, evidence:[...] }
       body(constraint): { rule_type:"constraint", action_pattern:<regex str>,
                           verdict:"allow"|"deny"|"escalate", authority,
                           severity:"low"|"medium"|"high"|"critical",
                           check_tier:"deterministic"|"classifier" }
       -> 202 { proposal_id, status:"accepted" }

POST /v1/wisdom/check               constraint enforcement - SYNC (agent waits)
       body: { action: {...} }       # the action the agent is about to take
       -> { verdict:"allow"|"deny"|"escalate", matched_rule?:id, reason?, check_tier }
```

### Memory
```
POST /v1/memory/episodic         ->  emit an episodic event (fire-and-forget)
       body: { agent_id, session_id, event_type, event_data }
       -> 202 { event_id }

GET  /v1/memory/episodic            windowed, team-scoped batch read (e.g. for the distiller)
       ?since_ts= &since_id= &limit=500 &event_type= &agent_id= &order=asc
       -> { events: [ {id, agent_id, session_id, event_type, event_data, occurred_at} ... ],
           next_cursor: {ts, id} | null }
       # (since_ts, since_id) is an exclusive composite cursor on (occurred_at, id);
       # omit both to read from the start (order=asc) or end (order=desc). order=desc
       # returns newest-first and treats the cursor as an exclusive upper bound -
       # e.g. ?event_type=distiller_watermark&limit=1&order=desc fetches the single
       # latest event of that type in O(1) regardless of table size. Excludes archived rows.
       # 422 if only one of since_ts/since_id is given, since_ts isn't valid ISO8601,
       # limit is outside 1..2000, or order isn't "asc"/"desc".

GET  /v1/memory/context             assemble working context for a turn
       ?session_id= &subject= &situation.<key>=<value> ...
       # session_id/subject are KWIM-interpreted; situation.* is the open
       # team-defined situation dict, forwarded to wisdom-rule matching.
       # subject fills the knowledge slot two ways at once: exact `about` tag match
       # first, then a semantic KNN over the same facts (distance-capped by
       # retrieval.context_semantic_max_dist), deduped. coverage.knowledge splits the
       # count into tag_n / semantic_n so callers can see which half answered.
       -> { recent:[...turns], knowledge:[...facts], wisdom:[...rules] }   # packed for the prompt

GET  /v1/memory/semantic            semantic recall
       ?q= &limit= &meta.<key>=<value> ...
       # q given: KNN vector search, optionally metadata-filtered.
       # q omitted: metadata-only exact-match fetch (score=0.0).
       -> { items: [ {id, content, score, metadata} ] }

GET  /v1/memory/working/{session}/{key}     working-memory read   -> { value } | 404
PUT  /v1/memory/working/{session}/{key}     working-memory write  body: { value, ttl_seconds? }
```

### Proposals (status of any async write)
```
GET  /v1/proposals/{id}             -> { id, object_type, status:"accepted"|"committed"
                                          |"rejected"|"pending_review", detail }
```

---

## 3. The propose -> gate flow (how an async write actually lands)

1. `POST /propose` - the KWIM service validates the request shape (deterministic;
   no LLM), publishes a proposal to `kwim.<team>.{knowledge|wisdom}.proposed` on the
   `/kwim` vhost, returns `202 {proposal_id, status:"accepted"}`.
2. The governance gate (a consumer) processes it: evidence-threshold + conflict
   check -> either commit (append `<team>.commit_log` + materialize the FalkorDB
   node/edges) or route to the human-review queue.
3. `GET /proposals/{id}` reports the outcome. Most callers fire-and-forget.

"Accepted" != "committed" - acceptance means the proposal is well-formed and queued;
the gate decides commitment. This follows propose-don't-write discipline.

---

## 4. Synchronous propose - designed-for, not built

Async is the default. When a future case needs a blocking commit, add it without
changing the async core:
- `POST /propose?wait=true&timeout=<s>` - the service publishes as usual, then waits
  (correlation/reply on the bus) for the gate's resolution up to `timeout`, and
  returns the resolved status (`committed`/`rejected`/`pending_review`) instead of
  a bare `202`. On timeout it falls back to `202 {proposal_id}` (the work isn't
  lost - poll `/proposals/{id}`).

The `proposal_id` + status model already makes a sync wrapper just "propose then
wait for resolution," so this is purely additive.

---

## 5. Errors & versioning
- `200` sync OK - `202` async accepted - `400` bad shape - `401` bad/missing key -
  `403` team not permitted - `404` not found - `409` conflict. Body:
  `{ error: { code, message } }`.
- `/v1/` prefix; additive changes in place, breaking changes bump the version.

---

## 6. Open / provisional
- `wisdom/check`:  to surface enforcement-design problems early.
-  The constraint model is resolved (`action_pattern` regex;
  `verdict in allow|deny|escalate`; `check_tier in deterministic|classifier`, where
  `check_tier` is how the check runs). What's still open: the
  enforcement points beyond the tool boundary, and the `classifier` tier (needs
  the embedder) - this is the `deterministic` tier only.
- Intelligence / Tooling: Intelligence (LiteLLM) and Tooling are direct calls;
  unifying them under this contract (one tenancy/cost/governance surface) is a
  later goal.
- Universe reads: the team+universe merge happens server-side; no extra surface,
  but the query semantics (how universe results are tagged/merged) firm up when the
  universe is built with W.
