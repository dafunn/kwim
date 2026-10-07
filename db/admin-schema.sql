-- KWIM admin - cluster-wide console identity, key store, and audit trail.
-- Applied as kwim_user (schema owner). Not per-team: every table here holds
-- cross-team records.
--
-- Idempotent; re-applied on every deploy that changes it. A change to an existing
-- table goes in the retrofit section at the bottom (see docs/DESIGN.md,
-- "Provisioning teams").
CREATE SCHEMA IF NOT EXISTS kwim_admin;

-- Console operators. Password hashing is scrypt; `password_hash` stores the
-- encoded salt and derived key together, never a reversible value.
CREATE TABLE IF NOT EXISTS kwim_admin.operators (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      text        NOT NULL UNIQUE,
    password_hash text        NOT NULL,
    display_name  text,
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Server-side sessions. The token is never stored; only its sha256.
CREATE TABLE IF NOT EXISTS kwim_admin.sessions (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id uuid        NOT NULL REFERENCES kwim_admin.operators(id) ON DELETE CASCADE,
    token_hash  text        NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    user_agent  text
);

CREATE INDEX IF NOT EXISTS idx_admin_sessions_live
    ON kwim_admin.sessions (token_hash) WHERE revoked_at IS NULL;

-- Team API keys. `key_prefix` is the display handle, independent of the secret;
-- `key_hash` is the secret's sha256.
CREATE TABLE IF NOT EXISTS kwim_admin.api_keys (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    team         text        NOT NULL,
    label        text        NOT NULL,
    key_prefix   text        NOT NULL UNIQUE,
    key_hash     text        NOT NULL,
    capabilities text[]      NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   uuid        REFERENCES kwim_admin.operators(id),
    expires_at   timestamptz,
    revoked_at   timestamptz,
    last_used_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_admin_api_keys_live
    ON kwim_admin.api_keys (key_prefix) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_admin_api_keys_team
    ON kwim_admin.api_keys (team);

-- Append-only record of every console action. Never deleted by application code.
CREATE TABLE IF NOT EXISTS kwim_admin.audit_log (
    seq         bigserial   PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    operator_id uuid        REFERENCES kwim_admin.operators(id),
    action      text        NOT NULL,
    team        text,
    object_type text,
    object_id   text,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    result      text        NOT NULL CHECK (result IN ('ok','denied','error'))
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_at    ON kwim_admin.audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_team  ON kwim_admin.audit_log (team, at DESC);

-- Team registry: console metadata and lifecycle state for each team.
CREATE TABLE IF NOT EXISTS kwim_admin.teams (
    team              text        PRIMARY KEY,
    display_name      text,
    status            text        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','decommissioned')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        uuid        REFERENCES kwim_admin.operators(id),
    decommissioned_at timestamptz
);

-- Background jobs, one row per operation (rebuild, ...).
CREATE TABLE IF NOT EXISTS kwim_admin.jobs (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text        NOT NULL,
    team        text        NOT NULL,
    status      text        NOT NULL CHECK (status IN ('running','succeeded','failed')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    operator_id uuid REFERENCES kwim_admin.operators(id),
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- One running job per team and kind.
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_jobs_one_running
    ON kwim_admin.jobs (team, kind) WHERE status = 'running';

-- Retrofits - idempotent ALTERs for databases provisioned before a change to a
-- table above. Currently empty.
--
--   ALTER TABLE kwim_admin.<t> ADD COLUMN IF NOT EXISTS <col> <type>;
--   ALTER TABLE kwim_admin.<t> DROP CONSTRAINT IF EXISTS <name>;
--   ALTER TABLE kwim_admin.<t> ADD  CONSTRAINT <name> CHECK (...);
