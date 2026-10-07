import { client, unwrap } from "./client";
import type { ListEnvelope } from "./pagination";

export interface Proposal {
  proposal_id: string;
  object_type: "fact" | "rule";
  proposed_by: string | null;
  created_at: string;
  summary: string;
  body: Record<string, unknown>;
  resolved_at: string | null;
  resolution: string | null;
  resolved_by: string | null;
  resolved_via: string | null;
  /** No surface shows this today outside the console. */
  reject_reason: string | null;
}

export interface ProposalFilters {
  resolved?: "false" | "true" | "any";
  resolution?: string;
  object_type?: string;
}

export async function listProposals(
  team: string,
  filters: ProposalFilters,
  cursor: string | null,
  limit = 50,
): Promise<ListEnvelope<Proposal>> {
  return unwrap(
    client.GET("/v1/admin/teams/{team}/proposals", {
      params: {
        path: { team },
        query: { ...filters, cursor: cursor ?? undefined, limit },
      },
    }),
  ) as Promise<ListEnvelope<Proposal>>;
}

export interface ApproveProposalResult {
  status: "committed";
  object_id: string;
  seq: number;
}

export async function approveProposal(team: string, proposalId: string): Promise<ApproveProposalResult> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/proposals/{proposal_id}/approve", {
      params: { path: { team, proposal_id: proposalId } },
    }),
  ) as Promise<ApproveProposalResult>;
}

export async function rejectProposal(
  team: string,
  proposalId: string,
  reason?: string,
): Promise<{ status: "rejected" }> {
  return unwrap(
    client.POST("/v1/admin/teams/{team}/proposals/{proposal_id}/reject", {
      params: { path: { team, proposal_id: proposalId } },
      body: { reason },
    }),
  ) as Promise<{ status: "rejected" }>;
}
