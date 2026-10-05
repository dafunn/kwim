#!/bin/sh
# Export the mounted secret files as environment variables, then exec the given
# command. Used by the service's start command and by the operator CLIs.
set -e
export KWIM_PG_PASSWORD="$(cat /secrets/db-password)"
export KWIM_RMQ_PASSWORD="$(cat /secrets/rabbitmq-password)"
export KWIM_FALKOR_PASSWORD="$(cat /secrets/falkordb-password)"
export KWIM_API_KEYS="$(cat /secrets/api-keys)"
# Legacy capability lists (key-id prefixes for promote/seed and review), both
# from the promote-keys secret. Unset when the secret is absent.
export KWIM_PROMOTE_KEYS="$(cat /secrets/promote-keys 2>/dev/null || true)"
export KWIM_REVIEW_KEYS="$(cat /secrets/promote-keys 2>/dev/null || true)"
# mattermost review-surface secrets - optional until the operator
# provisions them; absence means notify-only/no-notify, not a startup failure.
export KWIM_MM_WEBHOOK_URL="$(cat /secrets/mm-webhook-url 2>/dev/null || true)"
export KWIM_MM_ACTION_SECRET="$(cat /secrets/mm-action-secret 2>/dev/null || true)"
exec "$@"
