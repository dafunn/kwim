import { client, unwrap } from "./client";

/** Never includes key_hash - list_api_keys() excludes it at the query. */
export interface ApiKey {
  id: string;
  team: string;
  label: string;
  key_prefix: string;
  capabilities: string[];
  created_at: string;
  created_by: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export async function listTeamKeys(team: string, includeRevoked = false): Promise<{ items: ApiKey[] }> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/keys", {
      params: { path: { team }, query: { include_revoked: includeRevoked } },
    }),
  ) as Promise<{ items: ApiKey[] }>;
}

/** Cross-team - Console administration's "all keys" view. */
export async function listAllKeys(opts: { team?: string; includeRevoked?: boolean } = {}): Promise<{ items: ApiKey[] }> {
  return unwrap(
    client.GET("/v1/admin/keys", {
      params: { query: { team: opts.team, include_revoked: opts.includeRevoked } },
    }),
  ) as Promise<{ items: ApiKey[] }>;
}

/** admin_key.py's CAPABILITIES - the only valid values the mint endpoint accepts. */
export const KEY_CAPABILITIES = ["read", "propose", "review", "promote"] as const;

export interface MintKeyResult {
  id: string;
  key_prefix: string;
  capabilities: string[];
  /** The full secret - present exactly once, in this response. Never persist it. */
  key: string;
}

export async function mintKey(
  team: string,
  body: { label: string; capabilities: string[]; expires_at?: string },
): Promise<MintKeyResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/keys", { params: { path: { team } }, body }),
  ) as Promise<MintKeyResult>;
}

export async function revokeKey(team: string, keyPrefix: string): Promise<{ id: string; revoked_at: string }> {
  return unwrap(
    client.DELETE("/v1/admin/teams/{team}/keys/{key_id}", {
      params: { path: { team, key_id: keyPrefix } },
    }),
  ) as Promise<{ id: string; revoked_at: string }>;
}
