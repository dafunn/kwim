# KWIM Data Model - FalkorDB graph + Postgres commit log

Two coupled artifacts: the FalkorDB graph model (the queryable projection:
K + W + provenance + semantic Memory) and the Postgres commit log (the
durable, replayable system-of-record the graph is rebuilt from). Documents and
skills also keep their content in Postgres tables of their own (sections 1.6
and 1.7); documents are projected into a separate graph (section 2.5).

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
    object_type   text        NOT NULL CHECK (object_type IN ('fact','rule','semantic','document','skill')),
    object_id     text        NOT NULL,                    -- the K fact / W rule (text, not uuid: seed path uses human-readable ids)
    operation     text        NOT NULL CHECK (operation IN ('commit','deprecate','reinforce','retract','confirm','amend',
                                                            'revise','retire','restore')),
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb, -- object content -> recreate the node
    provenance    jsonb       NOT NULL DEFAULT '{}'::jsonb, -- edges -> recreate the relationships
    proposed_by   text,                                    -- agent id
    source_kind   text        CHECK (source_kind IN ('agent_proposal','repo_sync','promotion','distiller')),
    gate_decision text        CHECK (gate_decision IN ('auto_committed','human_approved','human_retracted','human_confirmed'))
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
- `retract` / `confirm` - human post-hoc review of a committed object (status only).
  For a document, `retract` removes it from search and keeps its rows and files.
- `revise` - a new revision of a document, or a new version of a skill. The
  content is in the document's or skill's own tables; `payload` identifies it.
- `retire` / `restore` - a skill is hidden from agents, or brought back. Its
  versions are kept.
- `amend` - replace an object's content in place. `payload` carries the new values
  for the amended fields only; `provenance` carries
  `{amended_by, amended_via, previous_payload}`, where `previous_payload` holds
  those same fields' prior values. The node is overwritten, so that is the only
  surviving record of what it said before.

  Amend is content-only: `status`, `created_at`, `scope`, `evidence_count`, and
  every edge are out of reach, each having its own operation. Amendable fields are
  `statement`/`fact_type`/`about`/`decay_class` for a fact,
  `situation`/`approach` plus the constraint fields for a rule, and
  `content`/`metadata` for a semantic item. An object that is not live
  (fact not `current`, rule not `approved`) is refused, since amending something
  already superseded would edit a version the chain has moved past.

  Amend is not supersede. Supersede mints a new object with a `SUPERSEDES` edge and
  keeps the old text visible - the right shape for a change of belief. Amend
  overwrites - the right shape for a correction that should not mint a version.

`payload` shape (by object_type):
- semantic: `{content, metadata}` - a directly-written `:SemanticItem`
  (`POST /v1/memory/semantic`). Only this path is logged; bus-fed items keyed by
  episodic event id are re-derived from `<team>.episodic_events` instead, so
  logging them would duplicate a record replay already has. `gate_decision` is
  `auto_committed`: a semantic write passes through no screening, and the column
  admits no value meaning "ungoverned".
- fact: `{statement, fact_type, valid_from}`
- rule(advisory): `{rule_type:"advisory", situation, approach}`
- rule(constraint): `{rule_type:"constraint", action_pattern, verdict, authority, severity, check_tier}`
  `action_pattern` is a regex string; `verdict` in `allow|deny|escalate`;
  `check_tier` in `deterministic|classifier` (how the check runs - structured/regex
  vs. embedding+LLM; only `deterministic` is evaluated, and `classifier` constraints
  are skipped by `/check`). `severity` is a free string (`low|medium|high|critical` by
  convention).

- document: `{title, tags, revision_id, revision_number, label, sha256, mime_type,
  filename, byte_size}` for an upload or revision; a promotion's universe row
  carries `{title, tags, revision_id, sha256}` and `provenance`
  `{promoted_from_team, promoted_from_id, promoted_by, promoted_via, promoted_at}`.
- skill: `{name, version, description}`; the text is in `skill_versions`.
  `provenance` names the author or approver, the path (`via`), and for an
  approved proposal its id.

Document and skill rows are a record of what changed, who changed it, and when.
Their content lives in their own tables (sections 1.6 and 1.7), and the team
graph rebuild skips them.

`provenance` shape: `{proposed_by:<agent>, supported_by:[<episodic_event_id>...],
supersedes:<object_id?>, references:[<fact_id>...], about:[<entity_ref>...]}`. These
are exactly the edges to recreate in the graph.

Why this is enough to rebuild the graph: every node's content is in `payload`,
every node's edges are in `provenance`, and `seq` gives the order. Replay = for
each row in `seq` order, upsert the node and its edges.

---

## 1.5 Postgres freshness state - `<team>.fact_verifications`

```sql
CREATE TABLE IF NOT EXISTS <team>.fact_verifications (
    fact_id          text        PRIMARY KEY,
    last_verified_at timestamptz NOT NULL DEFAULT now(),
    verified_by      text
);
```

The durable home for `last_verified_at`, which the `:Fact` node carries but the
graph cannot retain across a rebuild. Written by `POST /v1/knowledge/facts/{id}/reaffirm`,
read by the rebuild's reapply step, and deleted with the fact by `forget`.

One row per fact, upserted - only the latest verification feeds freshness, so
per-reaffirm history would grow without a consumer. Deliberately not `commit_log`
rows: reaffirm asserts continued currency rather than a governed content change, and
it runs often enough to bloat a log that is replayed in full on every rebuild.

---

## 1.6 Postgres documents - `<team>.documents`, `document_revisions`, `document_figures`

| Table | Holds |
|---|---|
| `documents` | One row per document: `id`, `title`, `tags`, `status` (`ingesting`, `ready`, `failed`, `retracted`), `current_revision` (its newest ready revision), `promoted_to` on a promoted team document, `promoted_from_team` / `promoted_from_id` on a universe copy, created and updated stamps |
| `document_revisions` | One row per uploaded revision: `revision_number`, `label`, `sha256` of the original, `mime_type`, `filename`, `byte_size`, `page_count`, `extractor` and `extractor_version`, `extracted` (JSONB, the extraction every later step reads), `status`, `error`, upload and ingest stamps |
| `document_figures` | One row per figure cut from a revision: `figure_id`, `page`, `caption`, and `object_key`, the image's path on the documents volume |

The original files and figure images are on the documents volume, under
`<team>/originals/<sha256>` and `<team>/figures/<sha256>/`, written once and
never modified. `extracted` is a tree of sections (heading, level, pages,
children) holding blocks (paragraph, step, list, table, warning, caution, note,
figure reference, code), plus the figures. Each document keeps at most
`documents.max_revisions` revisions; past that the oldest revision's rows go,
and its files when nothing else refers to the same sha256.

---

## 1.7 Postgres skills - `<team>.skills`, `skill_versions`

| Table | Holds |
|---|---|
| `skills` | One row per skill: `id`, `name` (unique in the team), `status` (`current`, `retired`), `current_version`, `owner` (an operator account), `promoted_to` on a promoted team skill, `promoted_from_team` / `promoted_from_id` on a universe copy, created and updated stamps |
| `skill_versions` | One row per version, never updated except to add its embedding: `version`, the whole SKILL.md `text`, its parsed `description`, `body`, `agents`, `tags` and `refs`, `embedding` (of the whole skill; null until the embedder has run), `source` (`human`, `agent_proposal`, `promotion`), `author`, `approver`, `proposal_id` |

A GIN full-text index on the description and body serves search when the
embedder is unavailable. Agent proposals wait in `<team>.pending_proposals`
(`object_type` `skill`) until reviewed.

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

## 2.5 FalkorDB documents graph - graph `kwim_<team>_docs`

The current revision of each ready document, projected from its stored
extraction:

```
(:Document {id, title, revision, tags, status})
(:Section {id, heading, path, level, page_start, page_end})-[:PART_OF]->(:Section | :Document)
(:Chunk {id, seq, text, heading_path, page_start, page_end, block_types, embedding})-[:IN]->(:Section)
(:Chunk)-[:NEXT]->(:Chunk)
(:Figure {id, caption, page, object_key})-[:IN]->(:Section)
(:Chunk)-[:REFERS_TO]->(:Figure)
```

Section, chunk and figure ids are `<document id>:<local id>`, and every node
carries `document_id`, so a document is replaced or removed as a unit. Indexes:
`id` and `document_id` on each label, a vector index on `Chunk.embedding`
(dimension `documents.embedder.dim`, cosine, `efRuntime` 200), and a full-text
index on `Chunk.text` and `Chunk.heading_path`.

Rebuild: `python -m kwim_api.documents.rebuild` re-chunks, re-embeds and
re-projects every ready document from `document_revisions.extracted` into a
temporary graph and swaps it in. It reads Postgres only.

---

## 3. Replay / rebuild contract

Rebuild a team's graph from durable sources:
1. `CREATE`/clear graph `kwim_<team>`; create constraints + indexes.
2. Apply repo-synced facts from git (the canonical source for `source_kind=repo_sync`).
3. Replay `<team>.commit_log` `ORDER BY seq`: for each row, upsert the `:Fact`/`:Rule`
   node from `payload` and create edges from `provenance`; apply `deprecate`/
   `reinforce` as status/`evidence_count` updates; mark `supersedes` targets
   `superseded`. Each node's `created_at` is restored from the row's `committed_at`,
   so replay preserves object age rather than restamping it to the rebuild time.
4. Reapply `<team>.fact_verifications`: stamp `last_verified_at` onto each replayed
   `:Fact` still `current`. Runs after step 3, since the nodes must exist first.
5. Re-embed semantic source text (whose durable copy lives in episodic/git) into
   `:SemanticItem` + rebuild the vector index. Directly-written items come from the
   log in step 3 instead; that step runs first, so an episodic-derived item wins on
   any id collision.

Working memory is not rebuilt (ephemeral). Document and skill rows in the log
are skipped: documents have their own graph (section 2.5) and skills are read
from Postgres.

Both timestamps matter because freshness is computed from
`max(created_at, last_verified_at)`: without step 3's `committed_at` restore every
fact would rebuild as brand new, and without step 4 a recently verified fact would
revert to its creation age and read stale.

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

Documents and skills are promoted the same way, as copies with a promotion
record: a universe document (rows, files and extraction shared by content hash,
projected into `kwim_universe_docs`) or a universe skill version with `source`
`promotion`. The team copy records `promoted_to`. A team reads both scopes;
search returns a promoted document once, and a team skill hides a universe
skill of the same name.
