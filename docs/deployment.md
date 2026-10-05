# Deploying KWIM

How to stand up a KWIM stack from nothing. KWIM is **K**nowledge - **W**isdom -
**I**ntelligence - **M**emory + **T**ooling - a thin integration layer over
off-the-shelf infrastructure, not a monolith. This guide is the bring-up order and
what each step provisions; the authoritative detail lives in the manifests (`k8s/`)
and schema (`db/`) in this repo.

Every provisioning and cleanup step is something you should automate and codify
in your own tooling, never hand-run against a live cluster/DB/secret store. Keep that
discipline when you adapt this.

## What you're deploying

| Component | Role | Off-the-shelf |
|-----------|------|---------------|
| **FalkorDB** | K/W graph + per-team code graph + semantic vectors | yes (graph DB) |
| **PostgreSQL** | source-of-truth commit log + episodic memory | yes |
| **RabbitMQ** | internal governance bus (propose -> gate) | yes |
| **LiteLLM** | Intelligence - model gateway (routing + accounting) | yes |
| **kwim-service** | the K/W/M HTTP/JSON API + the gate + the code-graph extractor | this repo (`service/`) |
| **embedder** | sentence embeddings for the semantic + dedup paths | this repo |

The workload manifests are in `k8s/`. Apply them however you reconcile a cluster
(a GitOps controller, or `kubectl apply -k k8s/`).

## Prerequisites

- A **Kubernetes** cluster.
- Shared infra you can provision into: **PostgreSQL** (a superuser to create the
  DB/role), **RabbitMQ** (admin, to create a vhost/user), and a **container registry**.
- A secrets mechanism that can land values into the pod. The manifests expect secret
  files mounted at `/secrets/*`, loaded into the process env by `services/api/with-secrets.sh`
  at startup (a secret manager that syncs into the pod works well, but anything that
  populates those files does). Nothing puts a secret value in a manifest.
- For **Intelligence**: a model backend LiteLLM can route to - a local inference server
  and/or cloud API keys.

## Bring-up order

The order matters: each step provisions substrate the next depends on.

1. **Secrets.** Make these available at `/secrets/` (names from `with-secrets.sh`):
   `db-password`, `rabbitmq-password`, `falkordb-password`, `api-keys`, `promote-keys`,
   and - if you use the review surface - `mm-webhook-url`, `mm-action-secret`. If you run
   the code graph, also provision a read-only token for cloning the indexed repos.
2. **PostgreSQL substrate** (once). Create the KWIM database and the application role the
   service connects as. (The role needs normal DML including DELETE - the Forget path's
   preflight checks for it, and inline Forget deletes as this role.)
3. **RabbitMQ** (once). A dedicated vhost + user for the governance bus.
4. **Universe schema** (once per cluster). The shared cross-team `universe` schema
   (promoted, globally-approved Wisdom). FalkorDB's `kwim_universe` graph auto-creates on
   first write. (Schema shape: `db/`.)
5. **Admin schema + first operator** (if you run the admin console). Apply
   `db/admin-schema.sql` as the application role, then create the first operator
   with `python -m kwim_api.admin_bootstrap --username <name>` (see
   `operations.md`). The console cannot create its own first account.

   Re-apply the schema on every deploy that changes it, not once at bring-up. It
   is idempotent, so applying it unchanged is a no-op; wrap it in your deploy
   automation rather than running it from memory.

   `CREATE TABLE IF NOT EXISTS` adds a new table on a re-apply but is a no-op on
   one that already exists, so a new column or a widened CHECK needs a retrofit
   block in `db/admin-schema.sql` (its header states the convention).

   The service starts without this schema and keeps authenticating teams from
   `KWIM_API_KEYS`; the admin routes return 503 until it exists.
6. **Deploy the workloads.** Apply `k8s/` (see `k8s/kustomization.yaml`): FalkorDB,
   kwim-service, embedder, LiteLLM, the code-graph CronJob, and network policies. Your
   secrets mechanism materializes `/secrets`; `with-secrets.sh` loads them.
7. **Operator key.** Provision a seed/promote-capable API key for a team; its id-prefix
   goes into `promote-keys`, which gates `/wisdom/promote`, `/wisdom/seed`, and review.
8. **Provision your first team** (once per team). Two doors, same template: apply the
   Postgres schema from `db/team-schema.sql.j2` yourself (`<team>.episodic_events` +
   `<team>.commit_log`; the team's FalkorDB graph `kwim_<team>` auto-creates on first
   write), or - if you run the admin console (step 5) - `POST /v1/admin/teams` with
   `{team, display_name?}`, which applies the same template, initializes the graph
   immediately rather than waiting for first write, and writes the console's
   `kwim_admin.teams` record in the same call. A schema provisioned the first way
   before the console existed can be registered with it later via
   `POST /v1/admin/teams/{team}/adopt` (see `operations.md` -> "Add a team").
9. **(Optional) seed initial knowledge** for a team via the Knowledge/Wisdom API.
10. **(Optional) the code graph.** Set `REPOS` in `k8s/codegraph-extract-cronjob.yaml`
   as `name=owner/repo` pairs where the name is also the team the repo distills into
   (each repo gets its own `kwim_<name>_code` graph and proposes architecture facts into
   its own team's K/W). A daily CronJob runs it; trigger a one-off by creating a Job from
   the CronJob template. See `docs/operations.md` -> "add a repo".
11. **(Optional) the review surface.** Provide `mm-webhook-url` + `mm-action-secret`; the
    service then posts every proposal to a Mattermost channel with Approve / Reject / Forget
    buttons.

## Verify

- `kwim-service` is Running and `/health` is green.
- A team API key can `POST /v1/knowledge/propose` and the fact appears via
  `GET /v1/knowledge/facts`.
- `GET /v1/memory/context?subject=...` returns a warm-start bundle with coverage markers.

## How configuration flows

Configuration has two surfaces, split by concern:

- **Secrets + connection endpoints** are env vars, never files in the image:
  - **Secrets**: your secret store -> files at `/secrets/*` -> exported to `KWIM_*_PASSWORD`
    (and `KWIM_API_KEYS`, the capability allowlists, the review-surface secrets) by
    `with-secrets.sh` (the service entrypoint and the wrapper for one-off admin commands
    run via `kubectl exec`).
  - **Connection endpoints** (`KWIM_PG_HOST`, `KWIM_FALKOR_HOST`, `KWIM_RMQ_*`,
    `KWIM_EMBEDDER_URL`, `OTEL_*`, ...): plain env in `k8s/kwim-service.yaml`. Kept discrete
    (host/port/user as separate vars, never a URL DSN) because base64 passwords contain
    characters that corrupt URL parsing.
- **Tunables** - everything that isn't a secret or an endpoint (gate thresholds, decay
  half-lives, retrieval/warm-start sizes, the code-graph resolution confidence cascade and
  discovery scope, ...) - load in precedence order **env var -> `KWIM_CONFIG` file -> shipped
  defaults**:
  - Shipped defaults live in `services/api/kwim_api/kwim.defaults.yaml` (baked into the image). You
    don't edit that file - it's the base layer and may change on upgrade.
  - Point `KWIM_CONFIG` at your own YAML to override any subset; it is deep-merged over
    the defaults (set only the keys you care about; siblings are preserved).
  - Any single key can also be pinned by its `KWIM_*` env var (e.g. via the ConfigMap),
    which wins over both files - handy for a one-off without shipping a config file.

  See `operations.md` -> "Tune a configuration value" for the day-2 mechanics.

- **Tenancy**: per-team API key (Bearer) -> team derived server-side -> per-team Postgres
  schema + `kwim_<team>` graph; the `universe` graph holds promoted cross-team rules.

See `docs/operations.md` for day-2: provisioning teams, adding code-graph repos, the
governed-cleanup/forget tools, rebuilds, and the failure modes worth knowing.
