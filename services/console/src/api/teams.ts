import { client, unwrap } from "./client";

export interface TeamCounts {
  facts: number;
  rules: number;
  semantic: number;
  episodic: number;
  pending: number;
}

/**
 * GET /v1/admin/teams (admin_read.py's admin_teams() and admin_team_detail()).
 * has_schema=false: a console record with no schema. has_console_record=false:
 * a schema with no console record, which can be adopted.
 */
export interface TeamSummary {
  team: string;
  display_name: string | null;
  status: string | null;
  created_at: string | null;
  has_schema: boolean;
  has_console_record: boolean;
  counts: TeamCounts | null;
}

export interface TeamDetail extends Omit<TeamSummary, "counts"> {
  counts: TeamCounts;
  graph: string;
  code_graph: string;
  indexed_repos: string[];
  latest_commit_seq: number | null;
  latest_committed_at: string | null;
}

export async function listTeams(opts: { count?: boolean } = {}): Promise<{ items: TeamSummary[] }> {
  return unwrap(
    client.GET("/v1/admin/teams", { params: { query: { count: opts.count } } }),
  ) as Promise<{ items: TeamSummary[] }>;
}

export async function getTeam(team: string): Promise<TeamDetail> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}", { params: { path: { team } } }),
  ) as Promise<TeamDetail>;
}

export interface CreateTeamResult {
  team: string;
  schema_created: boolean;
  graph_initialized: boolean;
  already_existed: boolean;
}

/** 403 when team creation is disabled (KWIM_ADMIN_ALLOW_TEAM_CREATE); shown as returned. */
export async function createTeam(team: string, displayName?: string): Promise<CreateTeamResult> {
  return unwrap(
    client.POST("/v1/admin/teams", { body: { team, display_name: displayName } }),
  ) as Promise<CreateTeamResult>;
}

export async function adoptTeam(team: string): Promise<{ team: string; adopted: boolean }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/adopt", { params: { path: { team } } }),
  ) as Promise<{ team: string; adopted: boolean }>;
}

export async function decommissionTeam(team: string, reason: string): Promise<{ team: string; keys_revoked: number }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/decommission", { params: { path: { team } }, body: { reason } }),
  ) as Promise<{ team: string; keys_revoked: number }>;
}

export async function restoreTeam(team: string): Promise<{ team: string }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/restore", { params: { path: { team } } }),
  ) as Promise<{ team: string }>;
}

export interface DestroyPreviewResponse {
  preview_token: string;
  expires_at: string;
  counts: { facts: number; rules: number; semantic: number; episodic: number; commit_rows: number; pending: number };
  objects: { schema: string; graphs: string[] };
  preflight: Record<string, unknown>;
}

/** Requires the team decommissioned first (409 otherwise) - admin_teams.py's _require_decommissioned. */
export async function destroyTeamPreview(team: string): Promise<DestroyPreviewResponse> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/destroy/preview", { params: { path: { team } } }),
  ) as Promise<DestroyPreviewResponse>;
}

export interface DestroyResult {
  team: string;
  dropped: { schema: string; graphs: string[] };
}

/** Two typed confirmations, not one: the team name and the live commit-row count (TeamDestroyRequest). */
export async function destroyTeamExecute(
  team: string,
  body: { preview_token: string; confirm_team: string; confirm_commit_rows: number },
): Promise<DestroyResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/destroy", { params: { path: { team } }, body }),
  ) as Promise<DestroyResult>;
}

/** Labeled a recovery operation: the graph is reconstructed from the commit log; anything not in the log is not restored. */
export async function triggerRebuild(
  team: string,
  body: { skip_semantic?: boolean; in_place?: boolean } = {},
): Promise<{ job_id: string }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/rebuild", {
      params: { path: { team } },
      body: { skip_semantic: body.skip_semantic ?? false, in_place: body.in_place ?? false },
    }),
  ) as Promise<{ job_id: string }>;
}
