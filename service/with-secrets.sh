#!/bin/sh
# Source mounted secret files into env, then exec the given command.
# Single source of secret-loading for the uvicorn entrypoint and for
# one-off admin commands (e.g. python -m app.rebuild) run via kubectl exec.
set -e
export KWIM_PG_PASSWORD="$(cat /secrets/db-password)"
export KWIM_RMQ_PASSWORD="$(cat /secrets/rabbitmq-password)"
export KWIM_FALKOR_PASSWORD="$(cat /secrets/falkordb-password)"
export KWIM_API_KEYS="$(cat /secrets/api-keys)"
# Capability allowlists - key-id prefixes permitted to promote/seed and review.
# Operator-provisioned via your secret manager; fail-soft: if absent the vars stay unset and the
# service fail-closes (403) on /wisdom/promote, /wisdom/seed and /v1/review/* by
# design. Both derive from the one operator key today; to split review from
# promote, add a separate review-keys secret + mount and read it here.
export KWIM_PROMOTE_KEYS="$(cat /secrets/promote-keys 2>/dev/null || true)"
export KWIM_REVIEW_KEYS="$(cat /secrets/promote-keys 2>/dev/null || true)"
# mattermost review-surface secrets - optional until the operator
# provisions them; absence means notify-only/no-notify, not a startup failure.
export KWIM_MM_WEBHOOK_URL="$(cat /secrets/mm-webhook-url 2>/dev/null || true)"
export KWIM_MM_ACTION_SECRET="$(cat /secrets/mm-action-secret 2>/dev/null || true)"
exec "$@"
