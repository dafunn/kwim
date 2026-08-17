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
Create the per-team Postgres schema from the `db/` template (the `kwim_<team>` graph
auto-creates on first write), then provision a seed/promote API key for it.

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
  the live count drifted. (`--statement-contains` is the only way to separate same-metadata
  facts - e.g. forget contaminated code facts without taking real ones.)
- **`python -m kwim_api.reject_pending`** - bulk-resolve stale unresolved review proposals as
  `rejected` (filtered by `source_kind`), to clear queue clutter without a hard delete.
- **Reset a team's code graph** - drop `kwim_<team>_code` in FalkorDB, then re-extract.
  The extractor MERGEs and self-prunes per repo, but does not remove other repos'
  nodes from a shared graph - drop and rebuild when re-scoping which repos a team indexes.

### Rebuild the graph from the log
`python -m kwim_api.rebuild` replays `commit_log` to reconstruct a team's `kwim_<team>` graph.
The code graph is separate (`kwim_<team>_code`) so a rebuild never wipes it; it's
regenerated by the extractor, not the log.

## Observability

- LiteLLM + kwim-service emit OTEL traces; content logging is off so prompt/response
  text stays out of spans.
- `GET /v1/memory/context` returns coverage markers (`repo_not_indexed`, freshness) -
  an agent working blind is visible, not silent.

## Failure modes worth knowing (learned the hard way)

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
