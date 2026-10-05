# KWIM design

How KWIM's mechanisms work and why they are built that way. The parts and the
data flows are in [ARCHITECTURE.md](ARCHITECTURE.md). Code comments refer to the
sections here by name.

## The commit log is the source of truth

Every governed change is a row in `<team>.commit_log`, appended in `seq` order.
The FalkorDB graph is a projection of that log: `payload` holds a node's content
and `provenance` its edges, which is enough to rebuild it.

- **Log first, then graph.** The gate appends the row before it writes the node.
  If the graph write fails, the log still holds the change, and the next rebuild
  applies it. The reverse - a node with no row - would be dropped by a rebuild.
- **Ages survive a rebuild.** Replay restores each node's `created_at` from its
  row's `committed_at`, and writers set `created_at` only when a node is created.
  Without both, a rebuild would make every object look new and reset its
  freshness.
- **Verification stamps are separate.** `last_verified_at` lives in
  `<team>.fact_verifications`, one upserted row per fact, and the rebuild
  reapplies it after replay. It is not in the log: a reaffirm asserts that a fact
  is still current rather than changing it, it runs often, and the whole log is
  replayed on every rebuild. Only the latest stamp feeds freshness, so history
  would have no reader.
- **What is not replayed.** Working memory and proposal-status keys are
  ephemeral. Semantic items fed from the bus are re-derived from
  `episodic_events`; only directly written ones have log rows (see
  "Semantic memory"). Embeddings are not stored in the log, so a rebuild
  re-embeds with whatever model the embedder runs.
- **The code graph is not in the log.** Its source of truth is the
  repositories, so it lives in its own graph (`kwim_<team>_code`) that a
  rebuild never touches. A rebuild replaces `kwim_<team>` with what the log
  holds, which would wipe a code graph kept in the same graph.
- **Rebuilds run off to the side.** A rebuild builds a temporary graph, checks
  it, and swaps it in; `--in-place` clears and replays the live graph instead.
  A rebuild started from the console uses its own store connections, so it does
  not hold the service's connections for its whole run.

## The governance gate

Agents propose; the gate decides. It consumes proposals from the bus and either
commits them, rejects them, or routes them to a human.

| Kind | Decision |
|---|---|
| fact | evidence check, then an embedding screen: a near-identical fact is rejected as a duplicate, a close one goes to review, anything else commits |
| advisory | evidence check, then a count of distinct sessions behind the evidence (NELL-style); commits at `gate.auto_commit_threshold` sessions, otherwise review |
| constraint | always review; enforcement policy is not auto-committed |
| reinforce | adds evidence to an approved advisory without a new node; commits directly, since it makes no new claim |

- **Evidence must exist.** Every cited episodic event id is looked up in
  `episodic_events`; an unknown id routes the proposal to review. Ids that are
  not well-formed UUIDs are treated as unknown and never reach SQL: one
  malformed id (a model that truncated an evidence id) would otherwise fail the
  query's UUID cast and stop the consumer, and with it governance for every team.
- **The screen fails open.** If the embedder is down, the fact commits without a
  vector, marked `verify=skipped:embedder_unavailable`. The cost is a fact that
  semantic search cannot find, which `kwim_api.backfill_embeddings` repairs.
  Every commit path embeds the statement if it was not handed a vector, so no
  path can commit an unembedded fact while the embedder is up.
  `KWIM_GATE_VERIFY=0` turns the checks off.
- **The screen is scoped.** A fact is compared only with current facts of the
  same `fact_type` whose `about` set contains every one of the proposal's
  `about` refs, so two similar statements about different entities are not
  duplicates. The proposal's own `supersedes` target is excluded, so a changed
  statement replaces its predecessor rather than colliding with it.
- **The screen is not the read path.** The gate's duplicate search and the
  agents' semantic search are separate queries. Changing how retrieval matches
  must never change what the gate rejects.
- **Nothing commits silently.** Every auto-commit is posted for review after the
  fact, with Confirm and Retract buttons. Human-approved commits are not posted
  again.
- **Review is durable.** A proposal routed to review is written to
  `<team>.pending_proposals` before its status changes, so an approval always
  has a body to act on. Resolution is an atomic claim (`UPDATE ... WHERE
  resolved_at IS NULL RETURNING *`), so two reviewers cannot both resolve one
  proposal; the second gets 409. If the insert fails, the message is dropped and
  logged in full. Notifications are best-effort; the queue is the record.
- **Stable ids are dormant.** A proposal carrying its own `object_id` whose node
  already exists is skipped, and is screened by id rather than by similarity.
  Only raw-bus proposers can set it, and none does: the code distiller
  supersedes on change instead.

## Changing committed objects

| Operation | Effect | Reversible |
|---|---|---|
| supersede | a new object with a `SUPERSEDES` edge; the old one stays visible as `superseded` | yes, the chain is kept |
| amend | overwrites content in place; the log row keeps the previous values | the row is the only record of the old text |
| retract | status becomes `retracted`; reads stop serving it | yes, logged |
| confirm | stamps `confirmed_by` / `confirmed_at`; no status change | logged |
| forget | removes the object from every store | no |

- **Supersede for a change of belief, amend for a correction.** A typo fix
  should not create a permanent version, and a change of belief must not erase
  what the team believed before.
- **Amend touches content only.** Status, `created_at`, scope, evidence counts
  and edges each have their own operation; letting amend change them would make
  replay depend on operation order. An object that is not live is refused, since
  amending a superseded version would edit history the chain has moved past.
  The row's `previous_payload` holds only the replaced fields, and `reason`
  records why, so what changed, who changed it and why stay together in the log.
  The node's `commit_seq` moves to the amending row.
- **Promoted keys are kept in step.** Rule situations and semantic metadata are
  copied onto node properties so queries can filter on them. When an amend
  changes them, the old keys are removed first, so a stale key does not keep
  matching.

## Forget

Forget is the only hard delete. It removes an object's node and embedding, its
`commit_log` rows, its verification row, and the episodic events that supported
it and nothing else, leaving no tombstone for a rebuild to restore.

- **Shared evidence is kept.** An episodic event that also supports an object
  outside the forget set is preserved (`--force-shared` overrides).
- **No half-forget.** A read-only Postgres preflight checks that the role can
  delete from every table involved before anything is deleted. Deleting the
  node while its log row survived would let the next rebuild bring it back.
- **Confirmation is a count.** The CLI is a dry run by default. Committing needs
  an interactive confirmation or `--confirm-count N`, which aborts if the plan no
  longer has exactly N objects. In the review surface, the reviewer's click is
  the confirmation.
- **Targeting.** `--statement-contains` is the only filter that separates facts
  with identical metadata.
- **Semantic items** have their own tool, `kwim_api.forget_semantic`. It takes
  exact ids only: it deletes the log row too, so an over-broad pattern could not
  be undone by a rebuild. A bus-fed item has no log row and returns on the next
  rebuild unless its episodic event is forgotten too.
- **Rejected proposals** can be forgotten from the review surface, which deletes
  their source events (with the same guard) so they cannot be re-derived.

### Preview and confirm in the console

Forget, bulk reject and team destroy are previewed first. The preview returns a
token, stored with a five-minute TTL and consumed with `GETDEL`, so it is
single-use and a failed confirmation still uses it up.

- The token holds the selection, not the plan. Execute derives the plan again
  and compares the operator's typed count with it: comparing with the stored
  plan would always match, since that is the number the operator was shown. The
  shared-evidence guard is also evaluated again, so an event that gained a
  dependant during the window is kept.
- A 409 on execute means the set changed. The console fetches a new preview and
  makes the operator confirm again; it never resends.

## Retrieval

- **Two ways to find a fact.** `knowledge/query` matches `about` tags, which is
  exact and requires knowing the tag. `knowledge/search` ranks by vector
  distance to free text, for when the caller does not. `score` is a cosine
  distance everywhere: lower is closer, identical is 0.
- **Filter, then score.** Filtered search scores only the filtered candidates.
  Asking the vector index first would apply the filter after its top-k cut, and
  a tag whose facts fall outside the global top k would return nothing.
- **Search never returns an empty list by accident.** If the embedder is down,
  `knowledge/search` returns 503: an empty list would read as "we know nothing
  about that".
- **Context unions both.** In `memory/context`, the knowledge slot is the exact
  tag matches followed by semantic matches, deduplicated, so the semantic half
  can only add. Semantic matches beyond
  `retrieval.context_semantic_max_dist` (0.6) are dropped: an unbounded k-nearest
  search always returns k results, however unrelated. Measured on real
  questions, the right fact for a named entity lands at 0.25-0.55 and the next
  best at 0.63 or more. Paraphrases without the entity land at 0.70-0.79, so no
  single cutoff serves both; re-measure before changing it, and if the embedding
  model changes. Coverage reports how many came from each half.
- **Context degrades per slot.** A failing slot comes back empty with its
  coverage marker set, and the rest of the bundle still returns.
- **Freshness.** A fact's `decay_class` (`permanent`, `slow`, `fast`) comes from
  its proposer, then a `fact_type` map, then `slow`. Its age is measured from
  `as_of`, the later of `created_at` and `last_verified_at`. With half-lives from
  configuration: under half a half-life is `fresh`, under one is `aging`, beyond
  that `stale`; `permanent` is always fresh. Query and context results sort
  fresh-first, stably, so relevance order holds within a band; search keeps
  distance order.
- **Admin reads are separate queries.** The console's listings (all statuses,
  cursors, free-text filters) use their own store methods, so the agent-facing
  queries keep their exact shape.

## Semantic memory

- **Two write paths.** Episodic events carrying text are embedded by the
  semantic consumer, keyed by the event id so redelivery cannot duplicate them;
  their durable source is `episodic_events`. `POST /v1/memory/semantic` writes an
  item directly; it goes through the gate so it gets a log row, its only durable
  record. A logged item replays before the episodic re-derivation, so an
  episodic item wins an id collision.
- **Embedding failures.** The consumer logs and acknowledges: the event is
  already in Postgres, and a rebuild re-derives the item.
- **Metadata keys** are copied onto node properties so queries can filter on
  them; keys that collide with the item's own properties stay in the metadata
  JSON only.
- **Long text.** all-MiniLM-L6-v2 reads 256 tokens. The embedder runs with
  `--auto-truncate`, so a long document is embedded from its start; the full text
  is still stored, and long documents are fetched by metadata.

## Identity and access

### Team keys

- **Format.** `kwim_<prefix>_<secret>`. The prefix is 12 random base32
  characters, generated independently of the secret: it is a display handle and
  audit id, never part of the credential. The secret is 32 random bytes,
  url-safe base64, stored only as its SHA-256; its entropy makes a slow hash
  unnecessary. Parsing splits at fixed offsets and checks both halves against
  their alphabets, so anything this service could not have minted is rejected
  before it reaches a hash or a query.
- **Capabilities.** `read`, `propose`, `review`, `promote`. A key grants exactly
  what it carries; no capability means none, never "anyone". Unknown capability
  names are rejected when a key is minted.
- **Resolution and caching.** A resolved key is cached in-process for
  `admin.key_cache_ttl_seconds`. A cache hit skips the database, never the
  secret comparison: the hash travels with the cached entry and every request is
  compared in constant time, so knowing a prefix is never enough. The console's
  revoke and decommission routes clear the cache entry; the CLI runs in another
  process and cannot, so its revocations take effect within one TTL.
  `last_used_at` is updated once per cache fill, best-effort, so bookkeeping
  never turns a valid key into a 500.
- **Legacy keys.** Keys in `KWIM_API_KEYS` keep working while `kwim_admin` is
  absent or the bearer value is not in the new format, with capabilities taken
  from the `KWIM_PROMOTE_KEYS` / `KWIM_REVIEW_KEYS` prefix lists. A well-formed
  key that is not in the store is a 401; it never falls back. Each legacy key
  logs once per process until imported, so the log going quiet means the import
  is done. `admin_import_keys` mints one new key per legacy entry, never one per
  team, so different grants are not merged. It writes each row before printing
  its key, one at a time, and skips rows already imported, so a partial run can
  be resumed and never prints a key that does not exist.

### Operators

- **A separate principal.** Operators sign in with a username and password and
  get a session; team keys and sessions live in different tables, resolve
  through different dependencies, and neither satisfies the other.
- **Passwords** are hashed with scrypt; a stored hash that cannot be parsed or
  derived fails the login instead of raising. Session tokens are stored only as
  their SHA-256.
- **Sessions** last `admin.session_ttl_hours`, extended on use up to
  `admin.session_max_age_days` from creation; a failed extension does not fail
  the request. The cookie's `Max-Age` covers the whole possible lifetime, and the
  server's `expires_at` is the authority.
- **The cookie** is `__Host-kwim_admin_session` with `Secure`, or, with
  `admin.secure_cookie` false, a plain name without `Secure` for deployments
  without TLS. The two go together because browsers drop both over plain HTTP,
  and they are configuration because the service cannot tell whether TLS
  terminated upstream. The insecure form is logged at startup.
- **The first operator** is created by `admin_bootstrap`, which refuses to run
  once one exists unless forced, and reads the password from a prompt or stdin,
  never an argument.

### Login protection

- **Lockout** counts failures per username and per source address, and locks
  the identity whether or not the next attempt would succeed. The source is the
  first `X-Forwarded-For` hop: behind an ingress every request comes from the
  proxy, and counting that would let anyone lock out every operator. A spoofed
  header evades only the per-source counter. Entries are dropped when their
  window empties, since usernames are attacker-supplied.
- **No timing or content difference.** An unknown or inactive username still
  runs a full scrypt derivation, and the response is the same either way.
- **Audit without secrets.** A failed login records the submitted username only
  if it names a real operator: a password typed into the username field must not
  be logged.

### Audit

Every console action writes exactly one `kwim_admin.audit_log` row: who, what,
which team and object, and the outcome, including denials and errors. Checks that
can deny a known operator - a malformed body, an unknown team - run inside the
audited block so the denial is recorded. A failed audit write is logged as a
warning and does not fail the request.

### The review surface

Any team key can read its own review queue; approving and rejecting need
`review`. Mattermost buttons call `/v1/review/mm-action` and
`/v1/review/committed-action`, which have no team key: they are authenticated by
a shared secret embedded in the button, and answer 403 when
`KWIM_MM_ACTION_SECRET` is unset.

## The admin API

- **Every change goes through the gate** - the same methods the review surface
  uses - never straight to a store.
- **Strict bodies.** Console request bodies reject unknown fields with 422. By
  default an unknown field is dropped, which turns a partial request into a
  reported success; a typo in bulk reject's `source_kind` filter would widen it
  from one proposer to the whole queue. Agent-facing models stay lenient, since
  rejecting extra fields there would break existing clients.
- **Identity before existence.** Team-scoped routes resolve the operator before
  checking the team, so an unauthenticated caller gets 401 and learns nothing
  about which teams exist.
- **Cursors are opaque.** Each list encodes its keyset (commit sequence, creation
  time, ...) as base64, so the console passes back what it was given. Every
  endpoint that returns a cursor accepts one; a malformed cursor is 422, never a
  silent restart. A page shorter than the limit returns no cursor, and a full
  page always does.
- **Rebuild jobs** are rows in `kwim_admin.jobs`, at most one running per team
  and kind. A job left running when the service restarts is marked failed at
  startup.
- **One replica.** The login counters, the key cache and job tracking are
  in-process, which is correct only while kwim-service runs one replica. Preview
  tokens are in FalkorDB and would survive a second replica.

## Provisioning teams

- **One template.** `db/team-schema.sql.j2` is the only per-team schema. The
  console renders it with a single substitution, not a template engine, and
  fails on any placeholder other than `kwim_team`. The team name is validated as
  an identifier before it is placed in the SQL.
- **One transaction.** The rendered script runs as one multi-statement
  execution inside an explicit transaction, so a failure leaves no partial
  schema. It runs without bind parameters, because psycopg only accepts several
  statements in one call on the simple query protocol.
- **Re-applied, not migrated.** Both schema files are idempotent and re-applied
  on deploy. `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so a
  change to an existing table goes in a retrofit block at the end of the file.
- **Reserved names.** A team cannot be named after a Postgres system schema,
  `kwim_admin` or `universe`.
- **Decommission and destroy are separate.** Decommission marks the team inactive
  and revokes its keys, touching no data. Destroy drops the schema and both graphs
  and requires a decommissioned team, so deleting data takes two deliberate
  actions. Destroy drops the graphs first: they can be rebuilt from the schema,
  and the reverse is not true. Teams provisioned outside the console can be
  adopted, and the registry is reconciled against the schemas that actually exist.

## The code graph

- **Separate graph, stable ids.** Each team's code graph is `kwim_<team>_code`.
  Node ids are qualified names (`repo:path::qualified.name`), so re-extraction
  updates nodes in place. The graph holds structure, signatures, summaries and
  embeddings, never file bodies.
- **Incremental.** Files whose xxh3 hash matches the graph are not re-extracted,
  but every file is parsed so calls across files still resolve. Files that
  disappear, or become excluded, are pruned.
- **Scope.** Python first. Test files are excluded, because fixtures distort
  call counts. `.gitignore` and `.cgignore` are honoured so vendored trees stay
  out.
- **Call resolution** is a cascade - same class, import map, same module, simple
  name, qualified suffix, nearest suffix match - and each edge carries a confidence and the strategy
  that produced it. Ambiguous calls land in the low-confidence tier; traversals
  and community detection exclude low-confidence edges.
- **Communities** are Louvain clusters over the call graph.
- **Distillation** proposes a small set of facts: a per-repo architecture summary
  (its load-bearing functions, by PageRank and cross-community bridging) and
  cross-repo interfaces. It reads the current fact first and proposes only when
  the statement changed, superseding the old one. Per-function detail is
  answered by the `/v1/code` reads instead.
- **Reads are recorded.** Each `/v1/code` read lands in episodic memory, so what
  an agent looked at is part of the record.

## The client and the distiller

- **The client is a side channel.** Agent-facing calls never raise and never
  block: without configuration or with KWIM unreachable they return empty
  defaults, and emits are fire-and-forget. A job whose whole purpose is KWIM
  work uses the strict entry points (`require_available`,
  `read_episodic(strict=True)`), because there a silent success looks healthy
  while doing nothing.
- **Model access** goes through LiteLLM. The model name must be configured;
  calls carry `x-litellm-tags` (`agent:<agent>` plus deployment and caller tags)
  so spend can be attributed. langchain is an optional extra; the base client
  needs only httpx.
- **The distiller's watermark** is an episodic event. It is read newest-first
  with `limit=1`, and advanced only after every proposal is submitted. A failed
  LLM call leaves the watermark in place so the window is retried; a reply that
  parses but has the wrong shape advances it, since retrying would loop.
- **Evidence references.** Events are shown to the model by small integer
  references, which models repeat reliably, and mapped back to event ids. A
  candidate with no resolvable evidence is dropped.

## The embedder image

The model is copied into the TEI image at build time, and the container runs
with `HF_HUB_OFFLINE=1`, so the pod needs no access to the Hugging Face Hub.
Changing the model's dimension (384) means changing `embedder.dim` and rebuilding
every graph, since the vector indexes are created at that size.

## The admin console

- **Same origin, cookie only.** The console calls the API on its own origin and
  holds no credentials: the session is an HttpOnly cookie, the login response's
  token is ignored, and nothing is written to browser storage (a build step
  fails if the bundle references it).
- **Errors are answers.** A 401 anywhere clears local state and returns to the
  login page; 403 shows the server's reason; 409 is shown as a real outcome, not
  retried.
- **Lists load a page per click**, following `next_cursor` until it is null.
- **nginx** repeats the security headers in every location block, because nginx
  drops inherited `add_header` directives in any location that sets its own.
  `index.html` is never cached and the hashed bundle is cached indefinitely.
  Unknown paths serve `index.html` for client-side routing.

## Configuration

Connection settings are discrete values, never URLs with inline passwords:
generated passwords contain `+ / = @`, which break URL parsing. Tunables are
layered YAML with environment overrides. Settings that standard libraries read
from the environment themselves (`OTEL_*`) and the legacy key map are read live
rather than from the frozen settings object.
