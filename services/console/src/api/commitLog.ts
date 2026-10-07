import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";

/**
 * Columns of {team}.commit_log (db/team-schema.sql.j2). For an `amend` row,
 * `provenance.previous_payload` holds the fields `payload` replaced.
 */
export interface CommitLogRow {
  seq: number;
  id: string;
  committed_at: string;
  object_type: "fact" | "rule" | "semantic";
  object_id: string;
  operation: "commit" | "deprecate" | "reinforce" | "retract" | "confirm" | "amend";
  payload: Record<string, unknown>;
  provenance: Record<string, unknown> & { previous_payload?: Record<string, unknown> };
  proposed_by: string | null;
  source_kind: "agent_proposal" | "repo_sync" | "promotion" | "distiller" | null;
  gate_decision: "auto_committed" | "human_approved" | "human_retracted" | "human_confirmed" | null;
}

export interface CommitLogFilters {
  object_id?: string;
  object_type?: string;
  operation?: string;
  source_kind?: string;
  gate_decision?: string;
  order?: "asc" | "desc";
}

export async function listCommitLog(
  team: string,
  filters: CommitLogFilters,
  cursor: string | null,
  limit = 100,
): Promise<ListEnvelope<CommitLogRow>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/commit-log", {
      params: {
        path: { team },
        query: { ...filters, cursor: cursor ?? undefined, limit },
      },
    }),
  ) as Promise<ListEnvelope<CommitLogRow>>;
}
