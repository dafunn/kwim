# Operating KWIM

Day-2 guide: keeping a running KWIM healthy, growing it, and cleaning up when
something goes wrong. Assumes a stack stood up per `docs/deployment.md`. The
operations below are the underlying actions - wrap each in whatever automation you
run; never hand-run them ad hoc.

**Two standing rules.** (1) Codify cluster/DB/secret-store mutations as repeatable
automation - don't hand-run them; the destructive tools here are dry-run-first and
count-gated for exactly this reason. (2) Never print a secret value; verify by
key-name/existence only.

## Routine

- **Deploys** - apply `k8s/` through your reconciler; image tags can be advanced by an
  image-automation controller or by committing tag bumps.
- **Code graph** - a CronJob (`k8s/codegraph-extract-cronjob.yaml`) extracts each
  configured repo into its own `kwim_<team>_code` graph and distills one
  architecture-summary fact per repo into that team's K/W. Trigger off-schedule by
  creating a Job from the CronJob template.
- **One-off admin commands** run through the secret wrapper, e.g.
  `kubectl exec <kwim-service-pod> -c kwim-service -- /app/with-secrets.sh python -m kwim_api.<cmd>`.
  Running the module directly skips `/secrets` loading and fails to authenticate.

## Common tasks

### Add a team
Two doors, same template (`db/team-schema.sql.j2`) - use whichever fits:

- **Playbook** - create the per-team Postgres schema from the `db/` template (the
  `kwim_<team>` graph auto-creates on first write), then provision a seed/promote API
  key for it.
- **Console** - `POST /v1/admin/teams` with `{team, display_name?}`. Applies the same
  template (the service renders and applies it directly),
  initializes the graph immediately instead of waiting for the team's first write, and
  writes the `kwim_admin.teams` console record in the same call. Retrying a call that
  applied the schema but failed before the console record was written is safe: it
  returns `201` with `already_existed: true` rather than erroring. `team` is rejected
  (`422`) if it fails the schema-identifier check or names a reserved schema
  (`public`, `information_schema`, `pg_catalog`, `pg_toast`, `kwim_admin`) or
  `universe`, which is the cluster-wide shared schema and is provisioned once, not
  per-team. Gated by `admin.allow_team_create` (`KWIM_ADMIN_ALLOW_TEAM_CREATE`,
  default `true`).

A team provisioned by the playbook has no console record until you register one:
`POST /v1/admin/teams/{team}/adopt` (404 if the schema doesn't actually exist). This
writes no DDL - it only makes the console aware of a team it didn't create.

Re-run the same provisioner (either door) over existing teams after any template
change - it is idempotent. A team whose schema predates `fact_verifications` keeps
working, but its `last_verified_at` stamps are not durable and are dropped by a
rebuild; the service logs a warning naming the schema when it hits that case.

**Decommission and destroy** are separate, deliberately: `POST
/v1/admin/teams/{team}/decommission` marks a team inactive and revokes its keys but
touches no data (reversible via `.../restore`); `POST
/v1/admin/teams/{team}/destroy` drops the schema and both FalkorDB graphs and cannot
be undone. See "Cleanup & forget" below for the full destroy flow and its ordering
requirement.

### Keep the admin schema current
Apply `db/admin-schema.sql` through your automation on every deploy that changes it,
not once at bring-up. It is idempotent; re-applying it unchanged is a no-op.

When an admin surface errors naming a `kwim_admin` relation, re-apply the schema
before looking anywhere else.

`CREATE TABLE IF NOT EXISTS` adds a new table on a re-run but is a no-op on one that
already exists, so a new column or a widened CHECK needs a retrofit block in the
file - its header states the convention.

### Manage team keys
Two doors: the CLI below, or the console's key routes once a team exists. Neither is a `psql` snippet:
the standing rule at the top of this page applies to `kwim_admin` exactly as it does
to everything else.

**Console** - `GET /v1/admin/teams/{team}/keys?include_revoked=false` to list;
`POST /v1/admin/teams/{team}/keys` with `{label, capabilities: [...], expires_at?}`
to mint (`capabilities` from `read`/`propose`/`review`/`promote`, unknown values
rejected rather than silently dropped); `DELETE
/v1/admin/teams/{team}/keys/{key_id}` (the key's `key_prefix`, not its uuid `id`) to
revoke. Mint refuses (`409`) on a decommissioned team. The mint response's `key`
field is the only time the secret is ever returned - shown once, never logged, never
recoverable afterward, same as the CLI. Unlike the CLI, **console revoke is
immediate**: it runs in the service process, so it clears the in-memory resolution
cache directly instead of waiting out the TTL.

**CLI** - runs through the secret wrapper, like the other admin modules:

```
kubectl exec <kwim-service-pod> -c kwim-service -- /app/with-secrets.sh \
  python -m kwim_api.admin_key list [--team <team>] [--include-revoked]
```

- **Mint** - `admin_key mint --team <team> --label <what it is for>` with a
  repeatable `--capability` (`read`, `propose`, `review`, `promote`; omit for a
  read-only key). The key is printed once. Only its sha256 is stored, so there is no
  lookup later: a lost key is revoked and replaced, never recovered. Treat the
  command's output as secret material and place it in your secret store immediately.
- **Revoke** - `admin_key revoke --key-prefix <prefix>`. The prefix is the non-secret
  handle shown by `list`. **Revocation from the CLI takes effect within one cache
  period, not instantly**: the service caches key resolutions in-process
  (`admin.key_cache_ttl_seconds`, 30s by default) and a separate process cannot clear
  that cache. The revoked key keeps working until the entry expires. If you need it
  dead immediately, restart the service pod.
- **Capabilities** are carried by each key, so two keys are never told apart by a
  shared prefix. An unset grant means read-only, never "anyone". Legacy keys still
  get theirs from the `KWIM_PROMOTE_KEYS` / `KWIM_REVIEW_KEYS` prefix allowlists.
- **Import legacy keys** - `admin_import_keys` moves `KWIM_API_KEYS` entries into the
  store, one row per entry (never one per team - collapsing them would re-conflate
  the capabilities). Dry-run by default and it mints nothing until `--commit`, so a
  preview is never mistakable for a real credential. Re-running skips what it already
  imported, so recovering from a partial run is safe. Key values cannot be carried
  across, so every entry gets a fresh key you must distribute.
- **Operators** - `admin_bootstrap` creates the first console account; it refuses to
  run once one exists unless `--force` is given. There is no console route for
  creating operator accounts.

Until a team's key is imported, it keeps authenticating from `KWIM_API_KEYS`. The
service logs one line per unmigrated key on its first use in each process (not per
request) naming the team - that log going quiet is how you know the migration is
complete.

### Read the admin audit log
Every console action writes one `kwim_admin.audit_log` row, including denials and
failures: who, what, which team, which object, and the outcome. Failed logins are
recorded too, with the submitted username only when it matches a real operator - a
password typed into the username box must not become a log entry.

Read it through the console's read API: `GET /v1/admin/audit`, newest first and
cursor-paginated.

### Add a repo to the code graph
Edit `REPOS` in `k8s/codegraph-extract-cronjob.yaml` - `name=owner/repo` pairs,
the name is also the KWIM team the repo distills into (so each repo gets its own
`kwim_<name>_code` graph and proposes into its own team's K/W). Append `@branch` for a
non-default branch. Ensure the clone token can read the repo. Deploy, then run a one-off
extraction to populate immediately.

### Tune a configuration value
Non-secret tunables (gate thresholds, decay half-lives, retrieval/warm-start sizes, the
code-graph resolution cascade and discovery scope) resolve as env var -> `KWIM_CONFIG`
file -> shipped `services/api/kwim_api/kwim.defaults.yaml`. Two ways to change one in production:
- **One key, quick:** set its `KWIM_*` env var in `k8s/kwim-service.yaml` (e.g.
  `KWIM_GATE_THRESHOLD`, `KWIM_CG_CONF_IMPORT_MAP`) and redeploy. Env wins over both files.
- **Several keys / keep them together:** ship a YAML file (mounted, e.g. via ConfigMap),
  point `KWIM_CONFIG` at it, and set only the keys you're overriding - it's deep-merged over
  the defaults, so unset keys keep their shipped values. Mirror the nesting of
  `kwim.defaults.yaml` (e.g. `codegraph: { resolution: { import_map: 0.9 } }`).

Don't edit `kwim.defaults.yaml` in a deployment - it's the in-image base layer and changes
on upgrade. Changes take effect on restart (config loads once at startup). Vector-index
dimension (`embedder.dim`) is special: it must match the embedder model and forces a
graph rebuild, since the FalkorDB vector indexes are created at that dimension.

### Review governance (the OUT crossing)
Every proposal the gate auto-commits or routes to review posts to mattermost:
- **Pending** -> Approve / Reject / Forget.
- **Auto-committed** -> Confirm / Retract / Forget (post-hoc - nothing commits silently).
- **Forget** removes the item from memory inline on the click - it is irreversible.
  For a committed object it hard-deletes the FalkorDB node + embedding, `commit_log`
  rows, and non-shared source episodics (nothing left for a rebuild to re-derive); for a
  pending proposal it rejects and deletes the non-shared source episodics. The
  shared-evidence guard still protects episodics that support other live objects, and a
  Postgres preflight aborts before any FalkorDB delete if the service role can't DELETE
  (so a permission gap can't half-forget).
- REST parity exists for scripting (`/v1/review/...`).

## Cleanup & forget (destructive - all gated)

The Postgres `commit_log` is the source of truth; the FalkorDB graph is a rebuildable
view. The admin modules (run via `with-secrets.sh`):

- **`python -m kwim_api.forget`** - the operator batch/one-off form of the same hard-removal the
  Forget button performs inline: delete governed objects from every store (FalkorDB node +
  embedding, `commit_log` rows, non-shared source episodics). Dry-run default with a
  read-only Postgres preflight (can this role DELETE?); `--select` with `--fact-type` /
  `--statement-contains` to target; commit needs `--commit --confirm-count N` and aborts if
  the live count drifted. (`--statement-contains` is the only way to separate facts with the
  same metadata - e.g. forget some code facts of a fact type while keeping the rest.)
- **`python -m kwim_api.reject_pending`** - bulk-resolve stale unresolved review proposals as
  `rejected` (filtered by `source_kind`), to clear queue clutter without a hard delete.
- **`python -m kwim_api.forget_semantic`** - hard-remove `:SemanticItem` nodes by exact id
  (no `--select`). Deletes the graph node and, for a directly-written item, its
  `commit_log` row - both, or the next rebuild replays the item back. Dry-run default
  with the same Postgres DELETE preflight as `forget`. A bus-fed item (keyed by
  episodic event id) has no log row and is re-derived from `episodic_events` on the
  next rebuild; forget that event with `kwim_api.forget` if it must not return.
- **Reset a team's code graph** - drop `kwim_<team>_code` in FalkorDB, then re-extract.
  The extractor MERGEs and self-prunes per repo, but does not remove other repos'
  nodes from a shared graph - drop and rebuild when re-scoping which repos a team indexes.

### Decommission versus destroy a team

Two different verbs, on purpose - conflating them
behind one flag is how someone deletes production data meaning to disable it:

- **Decommission** (`POST /v1/admin/teams/{team}/decommission`, `{reason}`) marks the
  team inactive and revokes every live key. It touches no team data - facts, rules,
  episodic memory, and the commit log are untouched and stay fully readable through
  the admin read API; the console just renders the team as inactive. Reversible:
  `POST /v1/admin/teams/{team}/restore` flips it back to active, but does not restore
  the revoked keys - mint new ones.
- **Destroy** (`POST /v1/admin/teams/{team}/destroy/preview` then `POST
  /v1/admin/teams/{team}/destroy`) drops the Postgres schema and both FalkorDB graphs
  (`kwim_<team>`, `kwim_<team>_code`). Irreversible. **Requires the team to already be
  decommissioned** - destroy refuses with `409` otherwise, naming the decommission
  endpoint - so destroying a team is always two separate operator actions, not one.

Destroy follows the same preview-then-confirm shape as `kwim_api.forget`'s
`--confirm-count`: preview returns a `preview_token` (single-use, expires in 5
minutes), the row counts it would drop, and a preflight (`can_drop`, based on schema
ownership/superuser - schemas have no DROP privilege to check directly the way
DELETE does). Executing requires the token plus `confirm_team` (must equal the team
name exactly) and `confirm_commit_rows` (must equal the preview's count) - either
mismatch, or a stale/reused token, is `409` and drops nothing. Execution order is
graphs first, then the schema: the graph is rebuildable from the schema, so a failure
partway through leaves the recoverable direction.

Destroy is off by default - `admin.allow_team_destroy`
(`KWIM_ADMIN_ALLOW_TEAM_DESTROY`, default `false`). Both destroy routes return `403`
naming the setting until an operator deliberately turns it on. Team creation has the
opposite default (`admin.allow_team_create`, `KWIM_ADMIN_ALLOW_TEAM_CREATE`, default
`true`) - it's the destructive direction that's locked down, not provisioning.

### Rebuild the graph from the log
`python -m kwim_api.rebuild` replays `commit_log` to reconstruct a team's `kwim_<team>` graph.
The code graph is separate (`kwim_<team>_code`) so a rebuild never wipes it; it's
regenerated by the extractor, not the log.

## Observability

- LiteLLM + kwim-service emit OTEL traces; content logging is off so prompt/response
  text stays out of spans.
- `GET /v1/memory/context` returns coverage markers (`repo_not_indexed`, freshness) -
  an agent working blind is visible, not silent.

## Failure modes worth knowing

- **MERGE never prunes.** An extractor exclusion (`.cgignore`) stops new nodes; it does
  not remove ones already written. The extractor self-prunes per repo; standing
  contamination needs a graph drop + re-extract.
- **Distillation must be idempotent.** Code facts carry a stable `object_id` (uuid5 of
  structural identity), so re-distill no-ops unchanged facts instead of minting
  duplicates. Don't reintroduce random ids on a recurring proposer.
- **NetworkPolicy isolation.** A new pod (Job/sidecar) reaching FalkorDB/Postgres/embedder
  needs an explicit egress policy and the right pod labels - default-deny refuses it
  silently (connection refused). One-off admin work is better run by `kubectl exec` into
  the service pod, which is already wired, than as a separate Job.
- **Append-only by convention, not grant.** The service role can DELETE (the forget
  preflight verifies it before touching anything) - append-only is enforced by the app
  only writing, not by revoked privileges. Inline Forget relies on this: the service role
  owns the team schema, so the button's hard-delete works without operator creds.
- **One platform vs. one component.** KWIM is the whole stack; `kwim-service` is the K/W/M
  API within it; Intelligence is LiteLLM. Keep that straight in config and docs.
