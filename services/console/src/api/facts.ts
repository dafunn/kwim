import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";
import type { CommitLogRow } from "./commitLog";

export interface Fact {
  id: string;
  statement: string;
  fact_type: string;
  status: "current" | "superseded" | "retracted";
  created_at: string;
  about: string[];
  decay_class: string;
  as_of: string;
  freshness: "fresh" | "aging" | "stale";
  last_verified_at: string | null;
  source_kind: string | null;
}

export interface FactProvenance {
  proposed_by: string | null;
  supported_by: string[];
  supersedes: string | null;
}

export interface FactDetail {
  fact: Fact;
  provenance: FactProvenance;
  /** Shape matches models.AuditVersion; kept loose since audit_fact() returns raw rows, not that model. */
  audit_chain: Record<string, unknown>[];
  commit_rows: CommitLogRow[];
}

export interface FactFilters {
  status?: string; // "any" | "current" | "superseded" | "retracted"
  fact_type?: string;
  about?: string[];
  source_kind?: string;
  q?: string;
}

export async function listFacts(
  team: string,
  filters: FactFilters,
  cursor: string | null,
  limit = 50,
): Promise<ListEnvelope<Fact>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/facts", {
      params: {
        path: { team },
        query: { ...filters, cursor: cursor ?? undefined, limit },
      },
    }),
  ) as Promise<ListEnvelope<Fact>>;
}

export async function getFact(team: string, factId: string): Promise<FactDetail> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/facts/{fact_id}", {
      params: { path: { team, fact_id: factId } },
    }),
  ) as Promise<FactDetail>;
}

export interface AmendFactBody {
  statement?: string;
  fact_type?: string;
  about?: string[];
  decay_class?: string;
  reason: string;
}

export interface AmendResult {
  object_id: string;
  seq: number;
  operation: "amend";
}

/** Overwrites in place - refused (409) if the fact isn't current (gate.py). */
export async function amendFact(team: string, factId: string, body: AmendFactBody): Promise<AmendResult> {
  return unwrap(
    client.PATCH("/v1/admin/teams/{team}/facts/{fact_id}/amend", {
      params: { path: { team, fact_id: factId } },
      body,
    }),
  ) as Promise<AmendResult>;
}

export interface SupersedeFactBody {
  statement: string;
  fact_type: string;
  about?: string[];
  evidence?: string[];
  source_kind?: string;
  reason: string;
}

export interface SupersedeResult {
  object_id: string;
  seq: number;
  supersedes: string;
}

/** Creates a new fact and marks this one superseded - the old text stays visible. */
export async function supersedeFact(
  team: string,
  factId: string,
  body: SupersedeFactBody,
): Promise<SupersedeResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/facts/{fact_id}/supersede", {
      params: { path: { team, fact_id: factId } },
      body,
    }),
  ) as Promise<SupersedeResult>;
}

export interface CreateFactBody {
  statement: string;
  fact_type: string;
  evidence?: string[];
  supersedes?: string;
  about?: string[];
  source_kind: "agent_proposal" | "repo_sync" | "distiller";
  decay_class?: string;
}

/** Commits directly (gate_decision="human_approved") - bypasses the propose/review queue, unlike the agent-facing path. */
export async function createFact(team: string, body: CreateFactBody): Promise<{ object_id: string; seq: number }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/facts", { params: { path: { team } }, body }),
  ) as Promise<{ object_id: string; seq: number }>;
}
