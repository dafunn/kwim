# KWIM architecture

What the parts are, where they run, and how data moves between them. The
reasons behind the design are in [DESIGN.md](DESIGN.md); the HTTP surface is in
[contract.md](contract.md) and the storage layout in [data-model.md](data-model.md).

## Components

| Component | What it is | Runs as |
|---|---|---|
| kwim-service | The K/W/M HTTP/JSON API, the governance gate, the semantic-memory consumer, and the admin API (`services/api/kwim_api`) | Deployment, one replica |
| Code-graph extractor | Indexes repositories into each team's code graph and distills facts from it (`kwim_api.codegraph`, same image as kwim-service) | CronJob |
| Distiller | Reads a team's episodic events and proposes durable facts and advisories (`services/distiller`) | CronJob, one run per team |
| Embedder | Hugging Face Text Embeddings Inference serving all-MiniLM-L6-v2 (384 dimensions), model baked into the image | Deployment |
| Admin console | Static single-page app over `/v1/admin` (`services/console`), served by nginx | Deployment |
| Python client | `kwim.py`, `llm_router.py`, `secret_reader.py` (`clients/python`), published as `kwim-client` | Library |
| PostgreSQL | System of record: episodic events, the commit log, verifications, the review queue, and `kwim_admin` | External |
| FalkorDB | The queryable graph: K/W nodes, vector indexes, code graphs, working memory, short-lived tokens | StatefulSet |
| RabbitMQ | The asynchronous write path, one topic exchange on the `/kwim` vhost | External |
| LiteLLM | The model gateway (Intelligence); agents and the distiller call it directly, not through kwim-service | External |

## Tenancy

A team is a schema in Postgres (`<team>`), a graph in FalkorDB (`kwim_<team>`,
plus `kwim_<team>_code` for its code graph), and a segment of every bus routing
key (`kwim.<team>....`). All teams share one namespace, one database and one
FalkorDB instance. The team is derived from the caller's API key on every
request and never read from a request parameter.

`universe` is a reserved pseudo-team: the `universe` schema and the
`kwim_universe` graph hold rules promoted from team graphs, which every team
reads alongside its own.

`kwim_admin` is one cluster-wide schema for the console: operators, sessions,
API keys, the audit log, the team registry and background jobs.

## The kwim-service process

`kwim_api.main` builds one FastAPI app. Its lifespan opens the stores, sweeps
orphaned job rows, and starts two in-process bus consumers, each on its own
channel:

- the **gate** (`gate.py`), consuming `kwim.<team>.knowledge.proposed` and
  `kwim.<team>.wisdom.proposed`;
- the **semantic consumer** (`semantic_consumer.py`), consuming
  `kwim.*.episodic` from the durable queue `kwim.semantic`.

Process-wide store handles live on `runtime.State`. Routers, one module per
surface under `routers/`:

| Router | Prefix | Callers |
|---|---|---|
| `knowledge` | `/v1/knowledge` | agents: query, search, fact, audit, propose, reaffirm |
| `wisdom` | `/v1/wisdom` | agents: rules, propose, check; operators: promote, seed |
| `memory` | `/v1/memory` | agents: episodic, context, semantic, working |
| `code` | `/v1/code` | agents: search, trace, architecture, changes |
| `proposals` | `/v1/proposals` | agents: proposal status |
| `review` | `/v1/review` | reviewers and Mattermost: pending, approve, reject, committed actions |
| `admin` | `/v1/admin/session` | console: login, logout |
| `admin_read` | `/v1/admin` | console: cross-team reads |
| `admin_write` | `/v1/admin` | console: governed changes, rebuild jobs |
| `admin_teams` | `/v1/admin` | console: team lifecycle, key management |

Stores, under `stores/`: `postgres.py` (per-team tables), `admin.py`
(`kwim_admin`), `falkor.py` (K/W graph, semantic items, working memory, tokens),
`falkor_code.py` (code graphs, mixed into the FalkorDB store), `bus.py` (the
publisher), and `pg_pool.py` (the connection pool both Postgres stores use).

## Data flows

### Proposing knowledge or wisdom

```
agent -> POST /v1/knowledge/propose or /v1/wisdom/propose
      -> status "pending" (FalkorDB KV, TTL) + publish kwim.<team>.<kind>.proposed
gate  -> decide: commit, reject, or review
   commit: append <team>.commit_log -> materialize the node and edges in kwim_<team>
           -> Mattermost post with Confirm / Retract
   review: insert <team>.pending_proposals -> Mattermost post with Approve / Reject / Forget
reviewer -> /v1/review/{id}/approve (or the Mattermost button) -> the same commit path
```

### Episodic and semantic memory

```
agent -> POST /v1/memory/episodic -> <team>.episodic_events (Postgres) + publish kwim.<team>.episodic
semantic consumer -> events carrying text -> embed -> :SemanticItem in kwim_<team>
agent -> POST /v1/memory/semantic -> gate.commit_semantic -> commit_log row, then the node
```

### Reading

- `GET /v1/knowledge/query` filters facts by tag (`about`) and type;
  `GET /v1/knowledge/search` ranks facts by vector distance to free text.
- `GET /v1/memory/context` assembles a turn's working context: knowledge (tag
  matches plus semantic matches), approved wisdom rules for the situation, recent
  episodic events, and a slice of the code graph, each with coverage markers.
- `POST /v1/wisdom/check` evaluates an action against approved deterministic
  constraints, team and universe merged.

### Code graph

```
CronJob init container: clone or fetch each repo into the cache volume
extract: discover files -> hash -> parse (tree-sitter) -> resolve calls -> write kwim_<team>_code
         -> prune removed files -> Louvain communities
distill: read the graph -> per-repo architecture summary + cross-repo interfaces
         -> publish knowledge.proposed (source_kind repo_sync) -> the gate
agent -> /v1/code/* reads -> each read also lands in episodic memory
```

### Distiller

```
CronJob (per team): read the last watermark -> read episodic events past it
  -> LLM via LiteLLM -> facts and advisories with evidence
  -> POST propose for each -> append a new watermark event
```

### Rebuild

`python -m kwim_api.rebuild` (or `POST /v1/admin/teams/{team}/rebuild`) builds a
temporary graph from Postgres: it replays `commit_log`, reapplies
`fact_verifications`, and re-embeds episodic text into semantic items, then
swaps the temporary graph in. The code graph is not touched.

### The admin console

The browser loads the console from its own Deployment and calls `/v1/admin`
on the same origin; the ingress routes `/` to the console and `/v1` and
`/health` to kwim-service. The operator authenticates with a session cookie.
Changes go through the same gate methods the review surface uses, and each
writes one `kwim_admin.audit_log` row.

## Configuration and secrets

- **Secrets** are files mounted under `/secrets`. `with-secrets.sh` exports them
  as environment variables and then runs the given command; the Deployment and
  the operator CLIs all start through it.
- **Connection settings** are discrete environment variables (host, port, user,
  database, vhost, password).
- **Tunables** are declared in `kwim_api/kwim.defaults.yaml`. A deployment
  overrides them with its own YAML (`KWIM_CONFIG`) or with environment
  variables; precedence is environment, then `KWIM_CONFIG`, then the defaults.
  `kwim_api/config.py` lists every setting the service reads.
- **Tracing** is OpenTelemetry, enabled by `OTEL_EXPORTER_OTLP_ENDPOINT` and a
  no-op without it.

## Repository layout

| Path | Contents |
|---|---|
| `services/api/kwim_api/` | The kwim-service package: app assembly, routers, stores, gate, auth, admin CLIs, rebuild and forget tools, code graph |
| `services/api/tests/` | The service's tests |
| `services/distiller/` | The distiller job and its tests |
| `services/console/` | The admin console (TypeScript, React, Vite) |
| `clients/python/` | The Python client |
| `db/` | `team-schema.sql.j2` (per-team schema) and `admin-schema.sql` (`kwim_admin`) |
| `k8s/base/` | Example Kubernetes manifests |
| `docs/` | This document, [DESIGN.md](DESIGN.md), the contract, the data model, deployment and operations |
