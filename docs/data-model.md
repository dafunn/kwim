# KWIM Data Model - FalkorDB graph + Postgres commit log

Two coupled artifacts: the FalkorDB graph model (the queryable projection:
K + W + provenance + semantic Memory) and the Postgres commit log (the
durable, replayable system-of-record the graph is rebuilt from).

The relationship is the spine of everything else: every governed write is a
commit-log row; the graph is the materialized view of replaying the log (+ git
for repo-synced facts). Lose the graph -> replay the log -> graph is back. That is
what makes FalkorDB a disposable projection (durability lives in Postgres).

---

## 1. Postgres commit log - `<team>.commit_log`

Append-only, per-team (alongside `episodic_events`). One row per governed change.
Ordered by `seq` for deterministic replay. Nothing is updated in place
(supersede-not-mutate); state is derived by replaying.

```sql
CREATE TABLE IF NOT EXISTS <team>.commit_log (
    seq           bigserial   PRIMARY KEY,                 -- monotonic replay order
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    committed_at  timestamptz NOT NULL DEFAULT now(),
    object_type   text        NOT NULL CHECK (object_type IN ('fact','rule')),
    object_id     text        NOT NULL,                    -- the K fact / W rule (text, not uuid: seed path uses human-readable ids)
    operation     text        NOT NULL CHECK (operation IN ('commit','deprecate','reinforce')),
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb, -- object content -> recreate the node
    provenance    jsonb       NOT NULL DEFAULT '{}'::jsonb, -- edges -> recreate the relationships
    proposed_by   text,                                    -- agent id
    source_kind   text        CHECK (source_kind IN ('agent_proposal','repo_sync','promotion')),
    gate_decision text        CHECK (gate_decision IN ('auto_committed','human_approved'))
);
CREATE INDEX IF NOT EXISTS idx_<team>_commit_object  ON <team>.commit_log (object_id, seq);
CREATE INDEX IF NOT EXISTS idx_<team>_commit_type    ON <team>.commit_log (object_type, committed_at);
```

Operations (supersession is encoded in provenance, not a separate op):
- `commit` - a new fact or rule. `payload` = full content; `provenance.supersedes`
  (optional) carries the id of the object this one replaces. Replay marks the
  superseded object's status.
- `deprecate` - a rule moves to `deprecated` (object_id only; W lifecycle).
- `reinforce` - bump a rule's `evidence_count` with new supporting evidence
  (object_id + the evidence refs in payload). The advisory-rule confidence path.

`payload` shape (by object_type):
- fact: `{statement, fact_type, valid_from}`
- rule(advisory): `{rule_type:"advisory", situation, approach}`
- rule(constraint): `{rule_type:"constraint", action_pattern, verdict, authority, severity, check_tier}`
  `action_pattern` is a regex string; `verdict` in `allow|deny|escalate`;
  `check_tier` in `deterministic|classifier` (how the check runs - structured/regex
  vs. embedding+LLM; v1 builds `deterministic` only). `severity` is a free string
  (`low|medium|high|critical` by convention). The enforcement-point design (where
  `/check` is called from, beyond the tool boundary) is the part still open - not the
  field schema.

`provenance` shape: `{proposed_by:<agent>, supported_by:[<episodic_event_id>...],
supersedes:<object_id?>, references:[<fact_id>...], about:[<entity_ref>...]}`. These
are exactly the edges to recreate in the graph.

Why this is enough to rebuild the graph: every node's content is in `payload`,
every node's edges are in `provenance`, and `seq` gives the order. Replay = for
each row in `seq` order, upsert the node and its edges.

---

## 2. FalkorDB graph model - graph `kwim_<team>`

One graph per team (graph-per-tenant). FalkorDB/Cypher is schema-on-write, so
labels/edges need no predeclaration; constraints, property indexes, and the
vector index are explicit and created at team-graph init.

### Node labels

| Label | Meaning | Key properties |
|---|---|---|
| `:Fact` | Knowledge fact | `id` (uuid), `statement`, `fact_type`, `status` (`current`/`superseded`), `source_kind`, `commit_seq`, `created_at`, `valid_from` |
| `:Rule` | Wisdom learned rule | `id`, `rule_type` (`advisory`/`constraint`), `status` (`pending`/`approved`/`deprecated`), `scope` (`team`/`universe`), `evidence_count`, `commit_seq`, `created_at`, + rule-type fields (`situation`/`approach` or `action_pattern`/`verdict`/`check_tier`/...) |
| `:Agent` | proposing identity | `id` |
| `:Evidence` | bridge to a Postgres episodic event | `id`, `episodic_event_id` (-> `<team>.episodic_events.id`), `occurred_at` |
| `:SemanticItem` | semantic Memory item | `id`, `content`, `embedding` (vector 384), `metadata`, `created_at` |

Agent-emitted nodes (defined now; populated by team instrumentation, not the gate):
`:Decision`, `:Output`. These cost nothing to define (schema-on-write) and are
not optional - they're what extends the invalidation cascade to the things
that actually shipped ("which outputs used the bad fact"). Population requires
teams to emit decision/output provenance (the team-architecture corollary).

Entity node (label defined now; resolution deferred): `:Entity` (what facts are
about - enables entity-graph traversal). The genuine cost is entity resolution
(deciding when two references are the same entity - dedup), which is real work.
Until that exists, facts carry entity references as properties; promoting them to
deduped `:Entity` nodes is the one true deferral.

### Edge types (the provenance web)

```
(:Fact)-[:SUPERSEDES]->(:Fact)         # supersede-not-mutate lineage
(:Fact)-[:SUPPORTED_BY]->(:Evidence)   # what backs this fact
(:Fact)-[:PROPOSED_BY]->(:Agent)
(:Rule)-[:SUPERSEDES]->(:Rule)
(:Rule)-[:LEARNED_FROM]->(:Evidence)   # outcomes the rule was distilled from
(:Rule)-[:PROPOSED_BY]->(:Agent)
(:Rule)-[:REFERENCES]->(:Fact)         # a rule that cites facts
# extension / team-emitted:
(:Fact)-[:ABOUT]->(:Entity)
(:Decision)-[:USED]->(:Fact|:Rule)
(:Decision)-[:PRODUCED]->(:Output)
(:Decision)-[:BY]->(:Agent)
```

### The founding queries this enables
- Audit ("why believed X at T"): from a `:Fact`, walk `SUPPORTED_BY` ->
  `:Evidence`, `PROPOSED_BY` -> `:Agent`, `SUPERSEDES*` for the version chain.
- Invalidation cascade ("source S wrong -> what's tainted"): from the
  `:Evidence`/`:Fact`, walk inbound `SUPPORTED_BY`, `REFERENCES`, and (when
  present) `USED` to reach affected facts, rules, decisions, outputs.
- Trust-weighting: count/aggregate a node's `SUPPORTED_BY` / `evidence_count`.

### Indexes / constraints (created at init)
- Unique on `id` for `:Fact`, `:Rule`, `:Agent`, `:Evidence`, `:SemanticItem`.
- Property indexes: `:Fact(status)`, `:Rule(status)`, `:Rule(rule_type)`.
- Vector index: `CREATE VECTOR INDEX FOR (s:SemanticItem) ON (s.embedding)
  OPTIONS {dimension:384, similarityFunction:'cosine'}` - semantic Memory recall.

### Not in the graph
Working memory is ephemeral TTL scratch - plain Redis keys
(`kwim:<team>:<session>:<key>`, `SET ... EX`) in the same FalkorDB instance, not
graph nodes. Not rebuilt on recovery.

---

## 3. Replay / rebuild contract

Rebuild a team's graph from durable sources:
1. `CREATE`/clear graph `kwim_<team>`; create constraints + indexes.
2. Apply repo-synced facts from git (the canonical source for `source_kind=repo_sync`).
3. Replay `<team>.commit_log` `ORDER BY seq`: for each row, upsert the `:Fact`/`:Rule`
   node from `payload` and create edges from `provenance`; apply `deprecate`/
   `reinforce` as status/`evidence_count` updates; mark `supersedes` targets
   `superseded`.
4. Re-embed semantic source text (whose durable copy lives in episodic/git) into
   `:SemanticItem` + rebuild the vector index.

Working memory is not rebuilt (ephemeral).

---

## 3.5 Cross-team sharing: the universe graph

Keyspace isolation is non-negotiable - graph-per-tenant exists precisely so no
information bleeds across teams (today's deployment may not need it; tomorrow's
team or another deployment will). So sharing is not solved by duplication
(projecting shared objects into each team graph - that's backwards: fan-out writes,
N copies, per-team rebuild on every shared change) and not by collapsing
everyone into one property-partitioned graph (that throws away the isolation).

Instead, shared knowledge gets its own first-class graph:

- `kwim_universe` - a shared graph, peer to the team graphs, holding promoted
  (`scope=universe`) objects. Durable home: `universe.commit_log` (a `universe`
  Postgres schema). "Universe" is just another tenant to the commit-log + rebuild
  machinery.
- Reads: a team's queries hit its own graph + the universe graph; the
  facade runs both and merges. A universe write is one write to the universe
  graph - teams see it at query time, no projection, no fan-out, no per-team rebuild.
- Rebuild: each graph is rebuilt independently from its own commit log
  (`replay(team_log)+git` for a team; `replay(universe_log)` for universe). No
  cross-graph replay.

Hierarchy (future): the natural extension is team -> world -> universe - teams
sharing a `world` graph while staying isolated from other worlds. Not implemented in
this current codebase; we do team -> universe only. The design is the same at
each level (a graph + a commit log + the facade querying the relevant set), so
adding a `world` tier later is additive.

### No cross-graph edges
FalkorDB can't put an edge between two graphs. So a team object that relates to a
universe object stores the universe object's id as a soft reference (a
property), not a traversable edge. Within either graph, traversal is native; a
traversal that crosses team<->universe is done by the facade in hops (query the
team graph, collect universe refs, query the universe graph). This is the accepted
price of keyspace isolation.

### Promotion to universe
Promotion is a human-gated governance action; promoting an object must not drag
its sensitive source with it. A team object's supporting evidence lives in
that team's private episodic store; copying it wholesale into the universe
would leak private data to every team.

Proposed shape (the sanitization is not fully solved yet):
- Promotion creates a new universe object whose provenance is the promotion
  record (approver, source team, timestamp, rationale) - not the raw
  team-private evidence. The original keeps its full provenance in the team graph;
  the universe copy does not inherit it. This is structural isolation: raw
  source can't leak to universe because it is never copied.
- If any evidence/justification is to be carried forward, it must pass a
  sensitivity validation at promotion - for now the approving human is that
  validation point (they review exactly what crosses the boundary); an automated
  sensitivity check is a later enhancement.
- Hard rule: promotion never auto-promotes source information. Anything beyond
  the object + promotion record requires explicit, validated inclusion.

This is purely additive to the per-team model - universe is one more
tenant (graph + commit log).
